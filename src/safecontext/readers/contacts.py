"""Map phone numbers and email addresses to contact names, read-only.

Primary source: the Contacts app's own SQLite stores (Core Data), opened with
``mode=ro`` + ``PRAGMA query_only``:

* ``~/Library/Application Support/AddressBook/AddressBook-v22.abcddb`` and
  ``…/AddressBook/Sources/<UUID>/AddressBook-v22.abcddb`` (one per account:
  iCloud, Google, Exchange, "On My Mac"). The ``v22`` file name has been the
  same since macOS 10.7; any ``AddressBook-v*.abcddb`` is accepted.
* Tables / columns used: ``ZABCDRECORD`` (``Z_PK``, ``ZFIRSTNAME``,
  ``ZLASTNAME``, ``ZMIDDLENAME``, ``ZNICKNAME``, ``ZORGANIZATION``),
  ``ZABCDPHONENUMBER`` (``ZOWNER``, ``ZFULLNUMBER``),
  ``ZABCDEMAILADDRESS`` (``ZOWNER``, ``ZADDRESS``). Checked on macOS 14, 15, 26.

Permission: ``~/Library/Application Support/AddressBook`` is protected by
macOS privacy controls. Full Disk Access (which Messages needs anyway) opens
it; see :func:`check_access`.

Fallback: the Contacts framework through PyObjC (``import Contacts``), used
only when the files can't be read. It needs the separate *Contacts* permission
(a one-time system prompt) and the ``pyobjc-framework-Contacts`` package, which
is **not** a current dependency (pywebview only pulls in Cocoa, Quartz,
Security, UniformTypeIdentifiers and WebKit). Without it the fallback is
skipped and :func:`load_contacts` returns what the files gave (maybe nothing).
"""
from __future__ import annotations

import locale
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import quote

import phonenumbers

from .imessage import AccessState, AccessStatus

__all__ = [
    "ContactBook",
    "FDA_SETTINGS_PATH",
    "CONTACTS_SETTINGS_PATH",
    "CONTACTS_SETTINGS_URL",
    "addressbook_dir",
    "check_access",
    "default_region",
    "load_contacts",
    "normalize_handle",
]

FDA_SETTINGS_PATH = "System Settings → Privacy & Security → Full Disk Access"
FDA_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
CONTACTS_SETTINGS_PATH = "System Settings → Privacy & Security → Contacts"
CONTACTS_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts"


def addressbook_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "AddressBook"


def _db_files(root: Path) -> List[Path]:
    files = sorted(root.glob("AddressBook-v*.abcddb"))
    files += sorted(root.glob("Sources/*/AddressBook-v*.abcddb"))
    return files


# --------------------------------------------------------------------------
# Normalization


def default_region() -> str:
    """Two-letter region for numbers saved without a country code."""
    try:
        from Foundation import NSLocale  # pyobjc-framework-Cocoa, already installed with pywebview

        code = NSLocale.currentLocale().countryCode()
        if code:
            return str(code).upper()
    except Exception:
        pass
    for value in (locale.getlocale()[0], os.environ.get("LC_ALL"), os.environ.get("LANG")):
        if value and "_" in value:
            region = value.split("_", 1)[1][:2].upper()
            if region.isalpha() and len(region) == 2:
                return region
    return "US"


def normalize_handle(handle: str, region: str = "US") -> Optional[str]:
    """Return a comparable key: lower-case email, E.164 phone number, or the
    raw digits/name of a short code or alphanumeric SMS sender."""
    if not handle:
        return None
    h = handle.strip()
    if h.lower().startswith(("mailto:", "tel:")):
        h = h.split(":", 1)[1]
    if "@" in h:
        return h.lower()
    try:
        num = phonenumbers.parse(h, region)
        if phonenumbers.is_possible_number(num):
            return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    digits = "".join(ch for ch in h if ch.isdigit())
    return digits or h.lower()


def _national_key(handle: str, region: str) -> Optional[str]:
    """Number without country code: matches a contact saved as '555 123 4567'
    when the default region guessed the wrong country."""
    try:
        num = phonenumbers.parse(handle, region)
    except phonenumbers.NumberParseException:
        return None
    n = str(num.national_number)
    return n if len(n) >= 7 else None


# --------------------------------------------------------------------------
# Contact book


class ContactBook:
    def __init__(self, region: Optional[str] = None):
        self.region = region or default_region()
        self._exact: Dict[str, str] = {}
        self._national: Dict[str, Optional[str]] = {}  # None marks an ambiguous key
        self.sources: List[str] = []  # where names came from (paths / "Contacts framework")

    def add(self, name: str, handles: Iterable[str]) -> None:
        for h in handles:
            key = normalize_handle(h, self.region)
            if not key:
                continue
            self._exact.setdefault(key, name)
            if "@" not in key:
                nat = _national_key(h, self.region)
                if nat:
                    prev = self._national.get(nat, name)
                    self._national[nat] = name if prev == name else None

    def lookup(self, handle: Optional[str]) -> Optional[str]:
        if not handle:
            return None
        key = normalize_handle(handle, self.region)
        if key in self._exact:
            return self._exact[key]
        if key and "@" not in key:
            nat = _national_key(handle, self.region)
            if nat:
                return self._national.get(nat)
        return None

    def __len__(self) -> int:
        return len(self._exact)


def _display_name(first, middle, last, nickname, org) -> Optional[str]:
    full = " ".join(p for p in (first, middle, last) if p)
    return full or nickname or org or None


def _read_store(path: Path) -> Iterator[Tuple[str, List[str]]]:
    uri = f"file:{quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = 1")
        names: Dict[int, str] = {}
        for pk, first, middle, last, nick, org in conn.execute(
            "SELECT Z_PK, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION FROM ZABCDRECORD"
        ):
            name = _display_name(first, middle, last, nick, org)
            if name:
                names[pk] = name
        handles: Dict[int, List[str]] = {}
        for owner, value in conn.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
            if value:
                handles.setdefault(owner, []).append(value)
        for owner, value in conn.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
            if value:
                handles.setdefault(owner, []).append(value)
        for pk, hs in handles.items():
            if pk in names:
                yield names[pk], hs
    finally:
        conn.close()


def check_access(root: Optional[os.PathLike] = None) -> AccessStatus:
    """Whether the AddressBook files can be read (no names are read)."""
    base = Path(root) if root is not None else addressbook_dir()
    p = str(base)
    no_perm = AccessStatus(
        AccessState.NO_PERMISSION,
        f"SafeContext needs Full Disk Access to show contact names. Open {FDA_SETTINGS_PATH} "
        "and turn on SafeContext, then quit and reopen it.",
        p,
        FDA_SETTINGS_PATH,
        FDA_SETTINGS_URL,
    )
    if not base.exists():
        return AccessStatus(AccessState.NO_DATA, "No contacts on this Mac.", p, FDA_SETTINGS_PATH, FDA_SETTINGS_URL)
    try:
        files = _db_files(base)
        if not files:
            os.listdir(base)  # an empty listing may itself be a refusal
    except PermissionError:
        return no_perm
    if not files:
        return AccessStatus(AccessState.NO_DATA, "No contacts on this Mac.", p, FDA_SETTINGS_PATH, FDA_SETTINGS_URL)
    try:
        with open(files[0], "rb") as f:
            f.read(16)
    except PermissionError:
        return no_perm
    except OSError as e:
        return AccessStatus(AccessState.ERROR, f"Could not read contacts: {e.strerror}", p, FDA_SETTINGS_PATH, FDA_SETTINGS_URL)
    return AccessStatus(AccessState.OK, "Contacts can be read.", p, FDA_SETTINGS_PATH, FDA_SETTINGS_URL)


def load_contacts(
    root: Optional[os.PathLike] = None,
    *,
    region: Optional[str] = None,
    use_framework: bool = True,
) -> ContactBook:
    """Build a ContactBook from every AddressBook store that can be read.

    Falls back to the Contacts framework only when no store could be read and
    ``use_framework`` is set (and pyobjc-framework-Contacts is installed).
    """
    base = Path(root) if root is not None else addressbook_dir()
    book = ContactBook(region)
    try:
        files = _db_files(base) if base.exists() else []
    except PermissionError:
        files = []
    for path in files:
        try:
            for name, handles in _read_store(path):
                book.add(name, handles)
            book.sources.append(str(path))
        except (sqlite3.Error, PermissionError):
            continue
    if not book.sources and use_framework:
        _load_from_framework(book)
    return book


def _load_from_framework(book: ContactBook, timeout: float = 30.0) -> bool:
    """Fill ``book`` via CNContactStore. Returns False if unavailable or denied."""
    try:
        import Contacts  # type: ignore  # pyobjc-framework-Contacts (not a dependency)
    except ImportError:
        return False
    store = Contacts.CNContactStore.alloc().init()
    entity = Contacts.CNEntityTypeContacts
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(entity)
    if status == Contacts.CNAuthorizationStatusNotDetermined:
        done = threading.Event()
        granted: List[bool] = []

        def handler(ok, _err):
            granted.append(bool(ok))
            done.set()

        store.requestAccessForEntityType_completionHandler_(entity, handler)
        done.wait(timeout)
        if not (granted and granted[0]):
            return False
    elif status != Contacts.CNAuthorizationStatusAuthorized:
        return False
    keys = [
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactMiddleNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactNicknameKey,
        Contacts.CNContactOrganizationNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
    ]
    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

    def each(contact, _stop):
        name = _display_name(
            contact.givenName(), contact.middleName(), contact.familyName(),
            contact.nickname(), contact.organizationName(),
        )
        if not name:
            return
        handles = [str(p.value().stringValue()) for p in contact.phoneNumbers()]
        handles += [str(e.value()) for e in contact.emailAddresses()]
        book.add(name, handles)

    ok, _err = store.enumerateContactsWithFetchRequest_error_usingBlock_(request, None, each)
    if ok:
        book.sources.append("Contacts framework")
    return bool(ok)
