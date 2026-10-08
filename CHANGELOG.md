# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- **Comment tools** for managing page comments:
  - `wiki_list_comments(path, page_id, locale)` — `comments.list` (takes
    `locale` + `path`, **not** `pageId`), returns id, content, author and
    createdAt. Accepts a page `path` or a numeric `page_id` (exactly one);
    with `page_id` the page path is resolved internally. Requires
    `read:comments` on the page.
  - `wiki_create_comment(page_id, content)` — `comments.create`, requires
    `write:comments`. The comment is attributed to the authenticated user.
  - `wiki_update_comment(comment_id, content)` — `comments.update`, requires
    `manage:comments`.
  - `wiki_delete_comment(comment_id)` — uses the `comments.delete` mutation
    (named `delete`, **not** `remove`), requires `manage:comments`.
- Client support for comments: `list_comments`, `create_comment`,
  `update_comment`, `delete_comment`. Comment permissions are page-rule
  permissions (`mayOnPage`), not API-route rights.
- Permission hints in error messages (`read:comments`, `write:comments`,
  `manage:comments`) and an `authorEmail` guard (returns `Forbidden` on the
  live instance, so it is not requested).

### Changed

- The MCP server now registers 21 tools (17 before).

## [1.2.0] - 2026-10-08

Fork release. This release fixes the locale handling (English is now only a
last-resort fallback, not the hard-coded default) and adds a complete asset /
file-manager tool cluster plus post-move verification.

### Added

- **Locale resolution** (`_resolve_locale`) with the precedence:
  1. explicit `locale` argument,
  2. new optional `WIKIJS_DEFAULT_LOCALE` environment variable,
  3. the wiki's primary locale (`localization.config.locale`, cached per client),
  4. hard fallback `en` (used only when nothing else can be determined).
- **Asset / file manager tools**:
  - `wiki_upload_asset(local_path, folder_path, folder_id)` — multipart upload
    to `POST {URL}/u` (fields `mediaUpload` file + `mediaUpload` JSON
    `{"folderId": N}`), returns sanitized filename, asset path, URL and a
    ready-to-paste markdown link. Requires `write:assets`.
  - `wiki_list_assets(folder_path, folder_id, kind)` — `assets.list`, requires `read:assets`.
  - `wiki_create_asset_folder(slug, parent_path, parent_id, name)` — `assets.createFolder`, requires `write:assets`.
  - `wiki_rename_asset(asset_id, filename)` — `assets.renameAsset`, requires `manage:assets`.
  - `wiki_delete_asset(asset_id)` — `assets.deleteAsset`, requires `manage:assets`.
- **Client support for assets**: `asset_folders`, `asset_list`,
  `asset_create_folder`, `asset_folder_id` (path walk with on-demand folder
  creation), `asset_rename`, `asset_delete`, `upload_asset`.
- **Post-move verification**: `move_page` re-reads the page afterwards and
  raises a descriptive error (expected vs. actual path/locale) on mismatch,
  detecting the dirty state caused by pages created with the wrong locale
  (missing from Git storage → later `ENOENT` on move/delete).
- Optional `locale` parameter on `wiki_search`, `wiki_get_page`,
  `wiki_list_pages`, `wiki_create_page`, `wiki_update_page`, `wiki_move_page`.
- Permission hints in error messages (`write:assets`, `read:assets`,
  `manage:assets`) and a locale hint on move failures.

### Changed

- `create_page` / `move_page` no longer default to `en`: the locale is resolved
  via `WIKIJS_DEFAULT_LOCALE` → site primary locale, with `en` only as a
  last-resort fallback.
- `update_page` preserves the page's current locale on partial updates and only
  resolves the default when the page has no locale.
- `wiki_create_page` no longer hard-codes `en` as a server-side fallback; the
  resolution is deferred to the client.
- `config.py`: new optional `WIKIJS_DEFAULT_LOCALE` environment variable
  (`WikiJSConfig.default_locale`); `validate_config()` still only requires
  `WIKIJS_URL` and `WIKIJS_API_KEY`.

### Fixed

- Pages created through the MCP server are now created in the wiki's primary
  locale, so they are written to Git storage and can be moved/deleted reliably
  (fixes `ENOENT: stat /data/repo/...` errors on `pages.move` / `pages.delete`).

### Notes

- Wiki.js lowercases asset folder slugs and sanitizes (lowercase + underscores)
  uploaded filenames. The MCP client mirrors this for reporting; the server
  remains authoritative.
- Assets are served at `GET /{assetPath}` and require `read:assets` — assets
  are not accessible for anonymous (guest) users.

## [1.1.0] - (upstream)

- Agent UX improvements: richer tool descriptions, new tools
  (`wiki_get_history`, `wiki_get_version`), editor/locale exposure on
  `wiki_create_page`, site-locale default when creating pages, system
  certificate store trust via `truststore`.
