"""Speech provider registry. See docs/PROVIDER-CONTRACT.md.

One provider = one object:

  id                       stable slug: the audio cache key and the meter tag.
                           Never rename once audio exists under it.
  ext, mime                the one self-contained file shape synth() returns
  needs_key                True if synth() cannot run without a credential
  list_voices(cred)        -> [Voice]
  synth(text, voice, opts, cred) -> Audio
  cost_per_1m_chars(cred)  -> float | None   (None = unpriced, NOT free)
  chunks(text)             -> int, how many pieces synth() will report progress on

The provider is handed a *resolved* credential and never learns which door it
came through (paste-key now, oauth later). It never holds the login. `opts`
carries the per-call knobs (rate, speed, model) plus an optional `report(n,total)`
callback; `cred` is {key, region, group} for the providers that need it.
"""

from ._text import chunk_text


class Voice:
    __slots__ = ("id", "label", "locale", "gender")

    def __init__(self, id, label, locale="", gender=""):
        self.id = id
        self.label = label
        self.locale = locale
        self.gender = gender

    def dict(self):
        return {"id": self.id, "label": self.label,
                "locale": self.locale, "gender": self.gender}


class Field:
    """One declarative settings widget. A provider ships no HTML; it declares
    the shape and the host draws the widget (docs/V2-ARCHITECTURE.md)."""

    __slots__ = ("name", "label", "kind", "default", "choices", "help", "shown")

    def __init__(self, name, label, kind="text", default=None, choices=None, help="",
                 shown=True):
        self.name = name
        self.label = label
        self.kind = kind                # text | password | number | bool | choice
        self.default = default
        self.choices = list(choices) if choices else None
        self.help = help
        # `shown` is whether the settings screen draws this field. A knob the
        # provider keeps (and still reads) can be a deployment decision rather
        # than a switch a person is supposed to flick; hiding it belongs in the
        # field's own declaration, not in a name check inside the host.
        self.shown = shown

    def dict(self):
        return {"name": self.name, "label": self.label, "kind": self.kind,
                "default": self.default, "choices": self.choices, "help": self.help,
                "shown": bool(self.shown)}


class Audio:
    """One self-contained audio file: we cache it to disk and concatenate it, so
    no streams and no sidecar headers. cost_usd is what this text would cost at
    our own account price; the host zeroes it for a BYO key."""

    __slots__ = ("bytes", "ext", "mime", "cost_usd")

    def __init__(self, bytes, ext, mime, cost_usd=None):
        self.bytes = bytes
        self.ext = ext
        self.mime = mime
        self.cost_usd = cost_usd


def get_provider(pid):
    """The one lookup that replaces the old if/elif on `provider`."""
    return REGISTRY.get(pid)


def report(opts, done, total):
    """Progress callback, if the caller asked for one."""
    cb = (opts or {}).get("report")
    if cb:
        cb(done, total)


from .local import Local          # noqa: E402
from .openai import OpenAICompat  # noqa: E402
from .azure import Azure          # noqa: E402
from .minimax import MiniMax      # noqa: E402

REGISTRY = {}
for _p in (Local(), OpenAICompat(), Azure(), MiniMax()):
    REGISTRY[_p.id] = _p

