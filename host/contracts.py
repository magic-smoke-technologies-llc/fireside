"""The three v2 plugin contracts. See docs/V2-ARCHITECTURE.md.

Shapes only. The host and every plugin agree on these, and neither side imports
the other's implementation: dataclasses for the data crossing the boundary, and
a base class per kind that documents the methods the host will call. A plugin is
not required to subclass these - the host checks for the methods it needs - but
subclassing is the shortest way to say "I am this contract".
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---- data crossing the boundary ------------------------------------------------

@dataclass
class Field:
    """One declarative settings widget. The host renders it; a plugin ships no HTML."""
    name: str
    label: str
    kind: str = "text"                 # text | password | number | bool | choice
    default: Any = None
    choices: Optional[List[str]] = None
    help: str = ""
    placeholder: str = ""

    def dict(self):
        """Plain data, so the host UI never has to know a plugin's class."""
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "default": self.default, "choices": self.choices,
                "help": self.help, "placeholder": self.placeholder}


@dataclass
class Chapter:
    title: str
    text: str


@dataclass
class Book:
    title: str
    chapters: List[Chapter] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None           # plugin-owned stable id, if it has one
    cover: Optional[bytes] = None      # the book's own cover image, if it has one
    cover_mime: str = ""


@dataclass
class Voice:
    id: str
    label: str = ""
    language: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Audio:
    data: bytes
    ext: str = "mp3"
    mime: str = "audio/mpeg"


class Refused(Exception):
    """A plugin's clean refusal: not a crash, a reason the caller can show.

    A scanned PDF has no text layer; a reader that cannot read the file should
    say so in the user's language, not raise a stack trace the host turns into
    a 500. `status` is the HTTP status the host should answer with (422 for
    "this file is readable as a file but not as a book").
    """

    def __init__(self, error: str, detail: str = "", status: int = 422, **extra):
        super().__init__(detail or error)
        self.error = error
        self.detail = detail
        self.status = status
        self.extra = extra

    def payload(self) -> Dict[str, Any]:
        out = {"error": self.error}
        if self.detail:
            out["detail"] = self.detail
        out.update(self.extra or {})
        return out


@dataclass
class Consent:
    ok: bool = True
    reason: str = ""


@dataclass
class Quote:
    """What the plugin says before a run. The host never invents a price."""
    chars_est: int = 0
    price: float = 0.0
    currency: str = "USD"
    eligible: bool = True
    balance: Optional[float] = None
    refusal_reason: str = ""
    authoritative: bool = True


@dataclass
class Report:
    """What the plugin says after a run. `unknown` stays unknown, never zero."""
    charge: str = "unknown"            # charged | unknown | pending
    chars_actual: Optional[int] = None
    cost: Optional[float] = None
    currency: str = ""
    duration_s: Optional[float] = None
    external_balance: Optional[float] = None
    reconcilable: bool = False


@dataclass
class StorageInfo:
    kind: str = "local"
    durable: bool = True
    url_ttl: Optional[int] = None


# ---- one base class per kind ---------------------------------------------------

class Reader:
    """Matches a request first, then reads it. Ordered: lower `order` runs first,
    and the generic HTTP reader declares a high order so it runs last."""
    id = ""
    order = 100

    def matches(self, req: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def read(self, req, fetch, cred) -> Book:
        raise NotImplementedError

    def settings(self) -> List[Field]:
        return []

    def credential_hosts(self) -> List[str]:
        return []

    def transport(self) -> Dict[str, Any]:
        return {}

    def consent(self, req) -> Consent:
        return Consent(True)


class Speech:
    """Declares the fields it needs, then synthesizes. `report` is the only
    authority on what a run cost; None from cost_per_1m_chars means unpriced,
    never free."""
    id = ""
    ext = "mp3"
    mime = "audio/mpeg"
    needs_key = True

    def list_voices(self, cred) -> List[Voice]:
        raise NotImplementedError

    def synth(self, text, voice, opts, cred) -> Audio:
        raise NotImplementedError

    def quote(self, ctx) -> Quote:
        raise NotImplementedError

    def report(self, ctx) -> Report:
        raise NotImplementedError

    def cost_per_1m_chars(self, cred) -> Optional[float]:
        return None


class Storage:
    """Opaque blobs behind refs. The host never assumes a path and never assumes
    the bytes are still there."""
    id = ""

    def put(self, data: bytes) -> str:
        raise NotImplementedError

    def has(self, ref: str) -> bool:
        raise NotImplementedError

    def get(self, ref: str) -> bytes:
        raise NotImplementedError

    def delete(self, ref: str) -> None:
        raise NotImplementedError

    def size(self, ref: str) -> int:
        """Bytes for a blob, or 0 when it is absent. Storage accounting uses
        this instead of trusting a recorded byte count. The default reads the
        blob; a backend that can answer more cheaply overrides it."""
        if not self.has(ref):
            return 0
        return len(self.get(ref))

    def info(self) -> StorageInfo:
        return StorageInfo()


# ---- adapters ------------------------------------------------------------------

def to_book(r):
    """Coerce a reader's return into a Book.

    The v1 sources return plain dicts ({title, chapters:[{title,text}], meta});
    the v2 host wants a Book. The reader adapters call this so both shapes are
    legal and the host only ever sees Book. An object that already answers
    `title`/`chapters` (a Book, or a test double) is passed through untouched.
    """
    if isinstance(r, Book) or hasattr(r, "chapters"):
        return r
    if isinstance(r, dict):
        chs = []
        for c in r.get("chapters") or []:
            if isinstance(c, dict):
                chs.append(Chapter(title=c.get("title", ""), text=c.get("text", "")))
            else:
                chs.append(c)
        return Book(title=r.get("title") or "Untitled", chapters=chs,
                    meta=r.get("meta") or {}, id=r.get("id"),
                    cover=r.get("cover"), cover_mime=r.get("cover_mime") or "")
    return r

def to_audio(a):
    """Coerce a synth() result into a contract Audio.

    The v1 providers return providers.Audio (.bytes); the v2 host wants
    host.contracts.Audio (.data). The speech adapters call this so the host only
    ever sees the contract shape.
    """
    if isinstance(a, Audio):
        return a
    if hasattr(a, "bytes"):
        return Audio(data=a.bytes, ext=getattr(a, "ext", "mp3"),
                     mime=getattr(a, "mime", "audio/mpeg"))
    return a
