from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .core import Bridge


def make_server(bridge: Bridge):
    server = FastMCP("OneCodex — 1C Designer")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    local = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

    @server.tool(annotations=read)
    def list_connections() -> list[dict]:
        """List configured connections AND bases discovered from the 1C launcher's list.
        Entries with configured=false need import_base first. Listing does not connect/export databases.
        """
        return bridge.list_connections()

    @server.tool(annotations=read)
    def discover_bases() -> dict:
        """List all bases registered in the current Windows user's 1C launcher and linked .v8i lists.
        Returns base_id, name, kind, location, platform version hint, existing connection_ids and source warnings.
        Does not connect to bases; actual configuration name/version and access are not verified.
        """
        return bridge.discover_bases()

    @server.tool(annotations=local)
    def import_base(base_id: str, user: str = "", password_env: str = "", platform: str = "") -> dict:
        """Add a selected discovered base as a local connection. Use base_id from discover_bases.
        Specify a user if needed, and only an environment-variable NAME for password_env; never send a password.
        Existing connections and credentials are preserved. No export or database write; allow_apply=false.
        """
        return bridge.import_base(base_id, user, password_env, platform)

    @server.tool(annotations=local)
    def add_connection(connection: str, kind: str, location: str, user: str = "", password_env: str = "", platform: str = "", extension: str = "") -> dict:
        """Add a manually specified database absent from the launcher list.
        kind=file uses a folder path, kind=server uses server\\base. Connection ID is Latin letters/digits/_/-.
        password_env is a variable NAME, not a password. Existing profiles aren't overwritten. No database writes.
        """
        return bridge.add_connection(connection, kind, location, user, password_env, platform, extension)

    @server.tool(annotations=local)
    def sync_sources(connection: str) -> dict:
        """Start exporting saved Designer configuration to local XML/BSL. Returns job_id; poll get_job.
        Unsaved editor text is not visible. Drafts are preserved. No database configuration update.
        """
        return bridge.sync_sources(connection)

    @server.tool(annotations=read)
    def get_job(job_id: str) -> dict:
        """Get a background operation's phase, completion result or failure. No waiting required."""
        return bridge.get_job(job_id)

    @server.tool(annotations=read)
    def search_metadata(connection: str, text: str = "", kind: str = "", offset: int = 0, limit: int = 50) -> dict:
        """Search local metadata by name/synonym; kind uses Designer XML names, e.g. Catalog, Document.
        Sync first. Results include exact object addresses and XML paths. Uses pagination.
        """
        return bridge.search_metadata(connection, text, kind, offset, limit)

    @server.tool(annotations=read)
    def get_metadata(connection: str, address: str) -> dict:
        """Read properties, attributes, tabular sections and other child metadata of an exact address.
        Use address from search_metadata; includes paths of associated BSL modules.
        """
        return bridge.get_metadata(connection, address)

    @server.tool(annotations=read)
    def list_modules(connection: str, text: str = "", offset: int = 0, limit: int = 100) -> dict:
        """List BSL module paths in the last successful snapshot. Search is by path substring."""
        return bridge.list_modules(connection, text, offset, limit)

    @server.tool(annotations=read)
    def read_module(connection: str, path: str, start: int = 1, limit: int = 200) -> dict:
        """Read a module page (one-based lines) and its full-file SHA256 for stage_module.
        Read all needed pages before replacing the complete module text.
        """
        return bridge.read_module(connection, path, start, limit)

    @server.tool(annotations=read)
    def search_code(connection: str, text: str, path_filter: str = "", limit: int = 100) -> dict:
        """Find literal text, case-insensitive, in BSL modules. Returns paths and one-based line numbers."""
        return bridge.search_code(connection, text, path_filter, limit)

    @server.tool(annotations=local)
    def stage_module(connection: str, path: str, text: str, expected_sha256: str) -> dict:
        """Stage COMPLETE replacement text of an existing BSL module in a local draft.
        Use SHA256 from read_module. Preserves baseline BOM and newline format. Does not write to 1C.
        """
        return bridge.stage_module(connection, path, text, expected_sha256)

    @server.tool(annotations=local)
    def patch_module(connection: str, path: str, old_text: str, new_text: str, expected_sha256: str) -> dict:
        """Stage a single exact replacement in the synchronized baseline module.
        The old fragment must occur exactly once; use SHA256 from read_module.
        Replaces this module's existing draft from baseline. Combine edits or use stage_module for multiple edits.
        Does not write to 1C. Preferred for a small edit in a large module.
        """
        return bridge.patch_module(connection, path, old_text, new_text, expected_sha256)

    @server.tool(annotations=local)
    def review_changes(connection: str) -> dict:
        """Show staged unified diffs and create a plan_id tied to the exact draft and baseline version.
        Show the changes to the user before applying. Review again after any draft edits.
        """
        return bridge.review_changes(connection)

    @server.tool(annotations=local)
    def discard_changes(connection: str) -> dict:
        """Clear the active local draft manifest; preserves baseline and applied archives. No 1C writes."""
        return bridge.discard_changes(connection)

    @server.tool(annotations=write)
    def apply_changes(connection: str, plan_id: str) -> dict:
        """WRITE TO 1C: import reviewed BSL modules into saved Designer configuration.
        Only after user authorizes applying this plan; profile must allow_apply.
        Checks baseline conflicts and saves a CF/CFE backup before loading.
        Close other Designer sessions for this database before applying: external writers are not locked.
        Returns job_id; poll get_job. It does NOT run UpdateDBCfg. Check failures don't roll back load.
        """
        return bridge.apply_changes(connection, plan_id)

    @server.tool(annotations=read)
    def check_configuration(connection: str) -> dict:
        """Start Designer integrity, reference and thin-client/server syntax checks. Returns job_id.
        Inspect the log output even when process exit code is zero. Checks the saved database sources.
        """
        return bridge.check_configuration(connection)

    return server
