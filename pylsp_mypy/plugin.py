"""
File that contains the python-lsp-server plugin pylsp-mypy.

Created on Fri Jul 10 09:53:57 2020

@author: Richard Kellnberger
"""
import ast
import atexit
import collections
import logging
import os
import os.path
import re
import tempfile
from configparser import ConfigParser
from pathlib import Path
from typing import IO, Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type:ignore

from mypy import api as mypy_api
from pylsp import hookimpl
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

line_pattern = re.compile(
    (
        r"^(?P<file>.+):(?P<start_line>\d+):(?P<start_col>\d*):(?P<end_line>\d*):(?P<end_col>\d*): "
        r"(?P<severity>\w+): (?P<message>.+?)(?: +\[(?P<code>.+)\])?$"
    )
)

log = logging.getLogger(__name__)

# A mapping from workspace path to config file path
mypyConfigFileMap: Dict[str, Optional[str]] = {}

settingsCache: Dict[str, Dict[str, Any]] = {}

tmpFile: Optional[IO[str]] = None

# In non-live-mode the file contents aren't updated.
# Returning an empty diagnostic clears the diagnostic result,
# so store a cache of last diagnostics for each file a-la the pylint plugin,
# so we can return some potentially-stale diagnostics.
# https://github.com/python-lsp/python-lsp-server/blob/v1.0.1/pylsp/plugins/pylint_lint.py#L55-L62
last_diagnostics: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)


def parse_line(line: str, document: Optional[Document] = None) -> Optional[Dict[str, Any]]:
    """
    Return a language-server diagnostic from a line of the Mypy error report.

    optionally, use the whole document to provide more context on it.


    Parameters
    ----------
    line : str
        Line of mypy output to be analysed.
    document : Optional[Document], optional
        Document in wich the line is found. The default is None.

    Returns
    -------
    Optional[Dict[str, Any]]
        The dict with the lint data.

    """
    result = line_pattern.match(line)
    if not result:
        return None

    file_path = result["file"]
    if file_path != "<string>":  # live mode
        # results from other files can be included, but we cannot return
        # them.
        if document and document.path and not document.path.endswith(file_path):
            log.warning("discarding result for %s against %s", file_path, document.path)
            return None

    lineno = int(result["start_line"]) - 1  # 0-based line number
    offset = int(result["start_col"]) - 1  # 0-based offset
    end_lineno = int(result["end_line"]) - 1
    end_offset = int(result["end_col"])  # end is exclusive

    severity = result["severity"]
    if severity not in ("error", "note"):
        log.warning(f"invalid error severity '{severity}'")
    errno = 1 if severity == "error" else 3

    return {
        "source": "mypy",
        "range": {
            "start": {"line": lineno, "character": offset},
            "end": {"line": end_lineno, "character": end_offset},
        },
        "message": result["message"],
        "severity": errno,
        "code": result["code"],
    }


def apply_overrides(args: List[str], overrides: List[Any]) -> List[str]:
    """Replace or combine default command-line options with overrides."""
    overrides_iterator = iter(overrides)
    if True not in overrides_iterator:
        return overrides
    # If True is in the list, the if above leaves the iterator at the element after True,
    # therefore, the list below only contains the elements after the True
    rest = list(overrides_iterator)
    # slice of the True and the rest, add the args, add the rest
    return overrides[: -(len(rest) + 1)] + args + rest


def didSettingsChange(workspace: str, settings: Dict[str, Any]) -> None:
    """Handle relevant changes to the settings between runs."""
    configSubPaths = settings.get("config_sub_paths", [])
    if settingsCache[workspace].get("config_sub_paths", []) != configSubPaths:
        mypyConfigFile = findConfigFile(
            workspace,
            configSubPaths,
            ["mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg"],
            True,
        )
        mypyConfigFileMap[workspace] = mypyConfigFile
        settingsCache[workspace] = settings.copy()


@hookimpl
def pylsp_lint(
    config: Config, workspace: Workspace, document: Document, is_saved: bool
) -> List[Dict[str, Any]]:
    """
    Call the linter.

    Parameters
    ----------
    config : Config
        The pylsp config.
    workspace : Workspace
        The pylsp workspace.
    document : Document
        The document to be linted.
    is_saved : bool
        Weather the document is saved.

    Returns
    -------
    List[Dict[str, Any]]
        List of the linting data.

    """
    settings = config.plugin_settings("pylsp_mypy")
    oldSettings1 = config.plugin_settings("mypy-ls")
    oldSettings2 = config.plugin_settings("mypy_ls")
    if oldSettings1 != {} or oldSettings2 != {}:
        raise NameError(
            "Your configuration uses an old namespace (mypy-ls or mypy_ls)."
            + "This should be changed to pylsp_mypy"
        )
    if settings == {}:
        settings = oldSettings1
        if settings == {}:
            settings = oldSettings2

    didSettingsChange(workspace.root_path, settings)

    if settings.get("report_progress", True):
        with workspace.report_progress("lint: mypy"):
            return get_diagnostics(workspace, document, settings, is_saved)
    else:
        return get_diagnostics(workspace, document, settings, is_saved)


def get_diagnostics(
    workspace: Workspace,
    document: Document,
    settings: Dict[str, Any],
    is_saved: bool,
) -> List[Dict[str, Any]]:
    """
    Lints.

    Parameters
    ----------
    workspace : Workspace
        The pylsp workspace.
    document : Document
        The document to be linted.
    is_saved : bool
        Weather the document is saved.

    Returns
    -------
    List[Dict[str, Any]]
        List of the linting data.

    """
    log.info(
        "lint settings = %s document.path = %s is_saved = %s",
        settings,
        document.path,
        is_saved,
    )

    live_mode = settings.get("live_mode", True)
    dmypy = settings.get("dmypy", False)

    if dmypy:
        dmypy_status_file = settings.get("dmypy_status_file", ".dmypy.json")

    args = ["--show-error-end", "--no-error-summary"]

    global tmpFile
    if live_mode and not is_saved:
        if tmpFile:
            tmpFile = open(tmpFile.name, "w", encoding="utf-8")
        else:
            tmpFile = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        log.info("live_mode tmpFile = %s", tmpFile.name)
        tmpFile.write(document.source)
        tmpFile.close()
        args.extend(["--shadow-file", document.path, tmpFile.name])
    elif not is_saved and document.path in last_diagnostics:
        # On-launch the document isn't marked as saved, so fall through and run
        # the diagnostics anyway even if the file contents may be out of date.
        log.info(
            "non-live, returning cached diagnostics len(cached) = %s",
            last_diagnostics[document.path],
        )
        return last_diagnostics[document.path]

    mypyConfigFile = mypyConfigFileMap.get(workspace.root_path)
    if mypyConfigFile:
        args.append("--config-file")
        args.append(mypyConfigFile)

    args.append(document.path)

    if settings.get("strict", False):
        args.append("--strict")

    overrides = settings.get("overrides", [True])
    exit_status = 0

    if not dmypy:
        args.extend(["--incremental", "--follow-imports", "silent"])
        args = apply_overrides(args, overrides)

        log.info("executing mypy args = %s via api", args)
        report, errors, exit_status = mypy_api.run(args)
    else:
        # If dmypy daemon is non-responsive calls to run will block.
        # Check daemon status, if non-zero daemon is dead or hung.
        # If daemon is hung, kill will reset
        # If daemon is dead/absent, kill will no-op.
        # In either case, reset to fresh state

        _, errors, exit_status = mypy_api.run_dmypy(["status", "--status-file", dmypy_status_file])
        if exit_status != 0:
            log.info(
                "restarting dmypy from status: %s message: %s via api",
                exit_status,
                errors.strip(),
            )
            mypy_api.run_dmypy(["restart", "--status-file", dmypy_status_file])

        # run to use existing daemon or restart if required
        args = ["run", "--"] + apply_overrides(args, overrides)

        log.info("dmypy run args = %s via api", args)
        report, errors, exit_status = mypy_api.run_dmypy(args)

    log.debug("report:\n%s", report)
    log.debug("errors:\n%s", errors)

    diagnostics = []

    # Expose generic mypy error on the first line.
    if errors:
        diagnostics.append(
            {
                "source": "mypy",
                "range": {
                    "start": {"line": 0, "character": 0},
                    # Client is supposed to clip end column to line length.
                    "end": {"line": 0, "character": 1000},
                },
                "message": errors,
                "severity": 1 if exit_status != 0 else 2,  # Error if exited with error or warning.
            }
        )

    for line in report.splitlines():
        log.debug("parsing: line = %r", line)
        diag = parse_line(line, document)
        if diag:
            diagnostics.append(diag)

    log.info("pylsp-mypy len(diagnostics) = %s", len(diagnostics))

    last_diagnostics[document.path] = diagnostics
    return diagnostics


@hookimpl
def pylsp_settings(config: Config) -> Dict[str, Dict[str, Dict[str, str]]]:
    """
    Read the settings.

    Parameters
    ----------
    config : Config
        The pylsp config.

    Returns
    -------
    Dict[str, Dict[str, Dict[str, str]]]
        The config dict.

    """
    configuration = init(config._root_path)
    return {"plugins": {"pylsp_mypy": configuration}}


@hookimpl
def pylsp_hover(
    config: Config,
    workspace: Workspace,
    document: Document,
    position: dict[str, int],
) -> dict[str, Any]:
    settings = settingsCache.get(workspace.root_path, {})
    dmypy = settings.get("dmypy", False)
    dmypy_status_file = settings.get("dmypy_status_file", ".dmypy.json")

    if not dmypy:
        return {}

    line = position.get("line", 0) + 1
    column = position.get("character", 0) + 1

    stdout, stderr, status = mypy_api.run_dmypy(
        [
            "--status-file",
            dmypy_status_file,
            "inspect",
            "--force-reload",
            "--include-span",
            "--include-object-attrs",
            "--union-attrs",
            "--limit=1",
            "--show=type",
            f"{document.path}:{line}:{column}",
        ]
    )

    if status != 0:
        if stderr:
            return {"contents": f"Exit code={status}:\n\n{stderr}"}

        return {}

    if " -> " not in stdout:
        return {"contents": stdout}

    pos, msg = stdout.split(" -> ", maxsplit=1)
    nums = pos.split(":")
    msg = msg.strip()

    if "None" in nums:
        return {"contents": msg}

    if msg.startswith('"'):
        msg = msg[1:-1]

    if msg != "overloaded function":
        msg = f"```python\n{msg}\n```\n"

    return {
        "contents": msg,
        "range": {
            "start": {"line": int(nums[0]) - 1, "character": int(nums[1]) - 1},
            "end": {"line": int(nums[2]) - 1, "character": int(nums[3]) - 1},
        },
    }


def init(workspace: str) -> Dict[str, str]:
    """
    Find plugin and mypy config files and creates the temp file should it be used.

    Parameters
    ----------
    workspace : str
        The path to the current workspace.

    Returns
    -------
    Dict[str, str]
        The plugin config dict.

    """
    log.info("init workspace = %s", workspace)

    configuration = {}
    path = findConfigFile(
        workspace, [], ["pylsp-mypy.cfg", "mypy-ls.cfg", "mypy_ls.cfg", "pyproject.toml"], False
    )
    if path:
        if "pyproject.toml" in path:
            with open(path, "rb") as file:
                configuration = tomllib.load(file).get("tool", {}).get("pylsp-mypy")
        else:
            with open(path) as file:
                configuration = ast.literal_eval(file.read())

    configSubPaths = configuration.get("config_sub_paths", [])
    mypyConfigFile = findConfigFile(
        workspace, configSubPaths, ["mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg"], True
    )
    mypyConfigFileMap[workspace] = mypyConfigFile
    settingsCache[workspace] = configuration.copy()

    log.info("mypyConfigFile = %s configuration = %s", mypyConfigFile, configuration)
    return configuration


def findConfigFile(
    path: str, configSubPaths: List[str], names: List[str], mypy: bool
) -> Optional[str]:
    """
    Search for a config file.

    Search for a file of a given name from the directory specifyed by path through all parent
    directories. The first file found is selected.

    Parameters
    ----------
    path : str
        The path where the search starts.
    configSubPaths : List[str]
        Additional sub search paths in which mypy configs might be located
    names : List[str]
        The file to be found (or alternative names).
    mypy : bool
        whether the config file searched is for mypy (plugin otherwise)

    Returns
    -------
    Optional[str]
        The path where the file has been found or None if no matching file has been found.

    """
    start = Path(path).joinpath(names[0])  # the join causes the parents to include path
    for parent in start.parents:
        for name in names:
            for subPath in [""] + configSubPaths:
                file = parent.joinpath(subPath).joinpath(name)
                if file.is_file():
                    if file.name in ["mypy-ls.cfg", "mypy_ls.cfg"]:
                        raise NameError(
                            f"{str(file)}: {file.name} is no longer supported, you should rename "
                            "your config file to pylsp-mypy.cfg or preferably use a pyproject.toml "
                            "instead."
                        )
                    if file.name == "pyproject.toml":
                        with open(file, "rb") as fileO:
                            configPresent = (
                                tomllib.load(fileO)
                                .get("tool", {})
                                .get("mypy" if mypy else "pylsp-mypy")
                                is not None
                            )
                        if not configPresent:
                            continue
                    if file.name == "setup.cfg":
                        config = ConfigParser()
                        config.read(str(file))
                        if "mypy" not in config:
                            continue
                    return str(file)
    # No config file found in the whole directory tree
    # -> check mypy default locations for mypy config
    if mypy:
        defaultPaths = ["~/.config/mypy/config", "~/.mypy.ini"]
        XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME")
        if XDG_CONFIG_HOME:
            defaultPaths.insert(0, f"{XDG_CONFIG_HOME}/mypy/config")
        for path in defaultPaths:
            if Path(path).expanduser().exists():
                return str(Path(path).expanduser())
    return None


@atexit.register
def close() -> None:
    """
    Deletes the tempFile should it exist.

    Returns
    -------
    None.

    """
    if tmpFile and tmpFile.name:
        os.unlink(tmpFile.name)

    if os.path.exists(".dmypy.json"):
        os.unlink(".dmypy.json")
