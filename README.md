# WikiJS MCP Server

An MCP server that connects Claude to your [Wiki.js](https://js.wiki/) instance. Search, read, create, update, move, and delete wiki pages through natural language — and manage assets (uploads, folders, files).

## Prerequisites

- Python 3.10+
- A Wiki.js instance with API access enabled
- A Wiki.js API key (Administration > API Access > New API Key)

## Installation

### Claude Code

```bash
claude mcp add wikijs \
  --scope user \
  -e WIKIJS_URL=https://your-wiki.com \
  -e WIKIJS_API_KEY=your-api-key \
  -- pipx run wikijs-mcp
```

Verify with `claude mcp list`.

### Other MCP clients

Add to your MCP client config:

```json
{
  "mcpServers": {
    "wikijs": {
      "command": "pipx",
      "args": ["run", "wikijs-mcp"],
      "env": {
        "WIKIJS_URL": "https://your-wiki.com",
        "WIKIJS_API_KEY": "your-api-key",
        "WIKIJS_DEFAULT_LOCALE": "de"
      }
    }
  }
}
```

You can substitute `pipx run wikijs-mcp` with `uvx wikijs-mcp` or install globally with `pip install wikijs-mcp` and use `wikijs-mcp` as the command.

## Locale configuration

Page operations need a locale. Without a locale hint, the server resolves one
in this order:

1. The explicit `locale` / `destination_locale` tool argument.
2. The optional `WIKIJS_DEFAULT_LOCALE` environment variable.
3. The wiki's primary locale (queried once per client from the instance).
4. A hard fallback of `de`.

Upstream versions hard-coded `en` as the default, which broke wikis whose
primary locale is not English: pages were created in the wrong locale, never
written to Git storage, and later `move`/`delete` failed with
`ENOENT: stat /data/repo/...md`. Set `WIKIJS_DEFAULT_LOCALE` to the primary
locale of your wiki (e.g. `de`) to make this deterministic without a round-trip
to the wiki. `update_page` preserves the page's current locale unless an
explicit `locale` is passed.

## Asset upload usage

Assets (files, images) are uploaded via the Wiki.js v2 multipart endpoint
`POST {WIKIJS_URL}/u`. The MCP server exposes:

- `wiki_upload_asset(local_path, folder_path, folder_id)` — upload a local file.
  Missing folders in `folder_path` are created on demand. Returns the
  sanitized filename, the asset path, the public URL and a ready-to-paste
  markdown link.
- `wiki_list_assets(folder_path, folder_id, kind)` — list files (`ALL`,
  `IMAGE`, `BINARY`).
- `wiki_create_asset_folder(slug, parent_path, parent_id, name)` — create a
  folder.
- `wiki_rename_asset(asset_id, filename)` / `wiki_delete_asset(asset_id)` —
  rename/delete a file.

Example for an agent prompt:

> Upload `/tmp/report-2026.pdf` to `team/manuals` and embed it in the page
> `team/manuals/report` on the wiki.

### Permission notes

- Listing/reading assets requires the `read:assets` permission.
- Uploading and creating folders requires `write:assets`.
- Renaming and deleting assets requires `manage:assets`.
- Assets are served at `GET /{assetPath}` and are **not** accessible to
  anonymous (guest) users — guests receive a `403` unless the asset's path is
  made public in Wiki.js.

### Slug / filename behavior (documented, not worked around)

Wiki.js lowercases asset folder slugs (`assets.createFolder` runs
`sanitize(slug).toLowerCase()`). The MCP client walks and creates folder paths
with lowercased slugs, exactly as the server stores them. Uploaded filenames
are sanitized server-side to lowercase with spaces/`,`/`;`/`#` replaced by
underscores (e.g. `Report Final (2).PDF` → `report_final_2_.pdf`); the client
mirrors this so it can report the expected final names.

## SSO / Authentik session auth (lokal)

Instead of an admin personal access token you can connect to Wiki.js as a
regular user via your SSO login (Authentik). The server then authenticates
every API request with the browser session's `jwt` token — Wiki.js may return
a refreshed token in the `new-jwt` response header, which the client adopts
and persists automatically.

### 1. Install the `login` extra (optional, only needed for the browser login)

```bash
uv tool install . --extra login      # or: pip install 'wikijs-mcp[login]'
playwright install chromium
```

### 2. One-time browser login

```bash
wikijs-mcp login                     # uses WIKIJS_URL; add --url to override
```

The command opens a browser window at `{WIKIJS_URL}/login`. On setups that
only expose OIDC the login page usually auto-redirects to Authentik or shows
an SSO button — both work: the command simply waits until Wiki.js sets the
`jwt` cookie and stores it:

```
~/.config/wikijs-mcp/session-token   (chmod 600)
```

Inspect the current state (never prints the token itself):

```bash
wikijs-mcp session-status
```

### 3. Configure the server for session auth

Set `WIKIJS_AUTH_MODE=session` and, optionally, the token via the
`WIKIJS_SESSION_TOKEN` environment variable. Resolution precedence:
**environment variable > token file**. With no env var the token file from
step 2 is used automatically. The `new-jwt` renewal loop always re-saves into
the token file (an env var keeps taking precedence until you update it), so
for long-lived setups prefer the token file and leave `WIKIJS_SESSION_TOKEN`
unset.

```json
{
  "mcpServers": {
    "wikijs": {
      "command": "wikijs-mcp",
      "env": {
        "WIKIJS_URL": "https://your-wiki.com",
        "WIKIJS_AUTH_MODE": "session"
      }
    }
  }
}
```

If the wiki refuses to start in session mode without a token, the error tells
you to run `wikijs-mcp login` or set `WIKIJS_SESSION_TOKEN`.

### Required role permissions

The Authentik group must be mapped (**mapGroups**) to a Wiki.js role that
grants at least:

- `read:pages`, `manage:pages` — search/read/create/update/move/delete pages
- `read:assets`, `write:assets`, `manage:assets` — list/upload/rename/delete
  assets

Work only inside a scratch area when testing — create pages/assets and delete
them again, leaving no residue.

### Security notes

- The token file is written with `chmod 600` and lives outside the repository
  — never commit it.
- `session-status` masks the token; `login` never prints it.
- Tokens expire; re-run `wikijs-mcp login` when requests fail with `401`.

## Tools

| Tool | Description |
|------|-------------|
| `wiki_search` | Full-text search across all wiki pages |
| `wiki_get_page` | Get a page by path or ID, with optional `metadata_only` and `include_render` modes |
| `wiki_list_pages` | List pages with optional tag filtering, sort order and locale filter |
| `wiki_get_tree` | Get the hierarchical folder/page tree structure |
| `wiki_create_page` | Create a new page |
| `wiki_update_page` | Update a page via full replacement or surgical find-and-replace (`edits`) |
| `wiki_move_page` | Move a page to a new path and/or locale, with post-move verification |
| `wiki_delete_page` | Delete a page |
| `wiki_list_tags` | List all tags used across the wiki |
| `wiki_get_site_info` | Get wiki site metadata (title, description, host) |
| `wiki_get_history` | Get page edit history with pagination |
| `wiki_get_version` | Retrieve a specific historical version of a page |
| `wiki_upload_asset` | Upload a local file as a wiki asset (returns markdown link) |
| `wiki_list_assets` | List assets in a folder, optionally filtered by kind |
| `wiki_create_asset_folder` | Create an asset folder (slug is lowercased) |
| `wiki_rename_asset` | Rename an asset (keep the same extension) |
| `wiki_delete_asset` | Delete an asset |

## Development

```bash
git clone https://github.com/jaalbin24/wikijs-mcp.git
cd wikijs-mcp
poetry install
poetry run pytest
```

## License

MIT
