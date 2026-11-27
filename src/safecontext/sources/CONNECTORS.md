# Writing a SafeContext connector

A connector copies data from one place (a chat app, an export file, a folder of
transcripts) into SafeContext. SafeContext scrubs every item on the Mac before
storing it, then serves the result to AI.

Anyone can write a connector. Connectors ship **inside** SafeContext: you open a
pull request, it gets reviewed, and it goes out in the next signed release.
SafeContext never loads code from outside the app. A connector sees raw data
before the scrubber does, so every one is reviewed.

## The rules

These protect the promise SafeContext makes to its users. A pull request that
breaks one is not merged.

1. **Read-only.** Never send, change or delete anything at the source.
2. **Nothing leaves the Mac.** No network calls, except to the service's own
   official API when the connector is an API connector (see below). No
   analytics and no "phone home".
3. **Everything goes through the scrubber.** Return `RawItem`s from `to_item`
   and let `ReaderSource` do the rest. Never write to the store, and never
   override `sync()` or `_syncer()`. The registry refuses a connector that does.
4. **Keep raw data out of logs.** Log ids and counts only, never message text,
   names, numbers or addresses.
5. **Ask, don't take.** Declare every permission in `requirements` with a plain
   `why`. Check access in `check_access()` without triggering a macOS prompt,
   and raise `NeedsPermission` with a `permission_help` entry that tells the
   user what to do.
6. **Only fixture data in tests.** Build fake databases or files in a temp
   folder (see `demo/fake_messages.py`). Never read real user data, not even
   your own.
7. **No new dependencies** without asking first in the pull request.

## How to write one

Start from `tests/example_connector.py`. It is a complete working connector
for a chat export file.

1. **The reader** goes in `src/safecontext/readers/<name>.py`. It knows the
   source's format and nothing about SafeContext: a function or class that
   yields plain dataclasses, oldest first, starting after a cursor.
2. **The connector** goes in `src/safecontext/sources/<name>.py`. Subclass
   `ReaderSource` and provide:

   | What | Notes |
   |---|---|
   | `id` | Lowercase, stable, never changes: items are stored under it |
   | `name`, `description` | Shown on the Sources screen |
   | `kinds` | One or more of `message`, `event`, `chat`, `transcript`. The scrubber picks its rules by kind, so new kinds aren't allowed |
   | `api_version` | `1`, the current `CONNECTOR_API_VERSION` |
   | `requirements` | What the connector needs, each with a reason for the user |
   | `link_prefixes` | Optional. `https://` deep links the app may open for an item |
   | `read(cursor)` | Records after `cursor` (`None` means everything), oldest first |
   | `to_item(record)` | One record to one `RawItem`, or `None` to skip it |
   | `cursor_of(record)` | A string that makes `read` resume after this record |
   | `check_access()` | Optional. Raise `NeedsPermission` when access is missing |
   | `unread_ids()` | Optional, for a source with a read state: ids of every item it shows as unread now (also set `RawItem.unread`). Read it, never change it at the source |

   Batching, scrubbing, storing, the cursor, locking, progress and error
   handling all come from `ReaderSource`.
3. **Register it:** add the class to `CONNECTORS` in `sources/__init__.py`,
   and remove its entry from `COMING_SOON` if it had one.
4. **Test it:** add a `ConnectorFixture` for it to
   `tests/test_connector_conformance.py` and put it in `FIXTURES`. The kit
   (`tests/connector_kit.py`) then checks first sync, resuming, a bad
   record, a crash while reading, undeclared kinds, permissions, disconnect,
   and that card numbers and one-time codes never reach the store's files.
   Add tests of your own for the mapping in `to_item`.
5. Run `.venv/bin/pytest`. Every test must pass.

## API connectors

Connectors for web services that need a sign-in (like Gmail and Google
Calendar) sync through their own API syncer and are not covered by the kit
yet. Open an issue before starting one, so the sign-in and token storage can
be designed with you.

## Pull request checklist

- [ ] Reader in `readers/`, connector in `sources/`, registered in `CONNECTORS`
- [ ] Conformance fixture added; `.venv/bin/pytest` passes
- [ ] Follows the rules above: read-only, no network, no raw logging, fixture data only
- [ ] Every requirement has a plain-language `why`
- [ ] New dependencies, if any, are explained
