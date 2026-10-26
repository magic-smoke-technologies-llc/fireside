"""The render job pipeline: book text -> per-chapter audio behind refs.

A render is per (chapter text, provider, voice, model, speed). The cache is
global on the server's storage layer, so the same chapter text spoken by the
same voice is one render no matter which user or which book asked for it. The
job row carries progress; the client knows nothing about the pipeline.
"""

import os
import threading
import time
import uuid

from host import chunks, library, pron
from host.db import now


# ---- what a render costs, before committing to it --------------------------
# Rough factors, corrected against measured renders (1.86M chars ~= 31h of
# audio, ~1.7GB, ~10h to synthesize on the CPU engine). The point is the right
# order of magnitude before a job measured in hours, not a promise. Correct
# these when a real render disagrees.
CHARS_PER_AUDIO_SEC = 16.0      # ~1000 chars/min of speech audio
AUDIO_BYTES_PER_SEC = 16000.0   # ~128 kbps mp3
DEFAULT_CHARS_PER_SEC = 60.0    # synth wall-clock when the provider is unknown
PROVIDER_CHARS_PER_SEC = {
    "azure": 200.0, "minimax": 120.0, "openai": 200.0, "local": 48.0,
}

# One render at a time: the CPU engine pegs the box, and two at once makes the
# whole tower unresponsive. A second job waits its turn instead of fighting.
_RENDER_LOCK = threading.Lock()


# ---- retry: one dropped request is not a dead book ------------------------
# A speech server can drop a single request (a 429, a reset socket, a cold
# GPU). That is transient, so a chunk is retried with backoff before it counts
# as a failure, and one failed chapter no longer aborts the rest of the book.
SYNTH_ATTEMPTS = int(os.environ.get("FIRESIDE_SYNTH_ATTEMPTS", "4") or 4)
SYNTH_BACKOFF = float(os.environ.get("FIRESIDE_SYNTH_BACKOFF", "1.5") or 1.5)


class Cancelled(Exception):
    """The user asked this render to stop; the worker unwinds quietly."""


# A running render parks a threading.Event under its job id, so a cancel that
# arrives on the request thread reaches the worker without a DB round trip on
# every chunk. The registry is process-local: a restart clears it, and any job
# still marked active at boot is swept to stalled and resumed by hand.
_CANCELS = {}
_CANCELS_LOCK = threading.Lock()


def cancel_token(job_id):
    with _CANCELS_LOCK:
        ev = _CANCELS.get(job_id)
        if ev is None:
            ev = threading.Event()
            _CANCELS[job_id] = ev
        return ev


def request_cancel(job_id):
    """Mark a job for cancellation. The worker stops before its next chunk."""
    cancel_token(job_id).set()


def is_cancelled(job_id):
    with _CANCELS_LOCK:
        ev = _CANCELS.get(job_id)
    return bool(ev is not None and ev.is_set())


def release_cancel(job_id):
    with _CANCELS_LOCK:
        _CANCELS.pop(job_id, None)


def _synth(speech, text, voice, opts, cred, attempts=None, backoff=None,
           should_stop=None, sleep=time.sleep):
    """Speak one chunk, retrying a dropped request with exponential backoff.

    `should_stop` is polled between attempts so a cancel ends the loop instead
    of waiting out the backoff. On the last failure the original exception is
    re-raised, and the caller decides whether to skip the chapter or stop.
    """
    attempts = SYNTH_ATTEMPTS if attempts is None else attempts
    backoff = SYNTH_BACKOFF if backoff is None else backoff
    attempts = max(1, int(attempts))
    last = None
    for n in range(attempts):
        if should_stop and should_stop():
            raise Cancelled()
        try:
            return speech.synth(text, voice, opts, cred)
        except Exception as e:
            last = e
            if n < attempts - 1:
                if should_stop and should_stop():
                    raise Cancelled()
                sleep(max(0.0, backoff) * (2 ** n))
    raise last


def synth_rate(provider):
    return PROVIDER_CHARS_PER_SEC.get(provider or "", DEFAULT_CHARS_PER_SEC)


def estimate(chars, provider=""):
    """Estimated time and disk for rendering `chars`, before the user commits."""
    chars = int(chars or 0)
    audio_s = chars / CHARS_PER_AUDIO_SEC
    return {
        "chars": chars,
        "audio_seconds": audio_s,
        "audio_bytes": int(audio_s * AUDIO_BYTES_PER_SEC),
        "synth_seconds": chars / synth_rate(provider),
        "chars_per_sec": synth_rate(provider),
    }


def sweep_stale(conn, stale_seconds=0):
    """Close out jobs whose worker is gone.

    At startup no render thread can be alive, so `stale_seconds=0` marks every
    running job interrupted. A positive window only touches jobs untouched for
    that long. Converted chapters are kept either way, so a resume is cheap.
    """
    ts = now()
    msg = "interrupted; converted chapters are kept, run the convert again to resume"
    if stale_seconds and stale_seconds > 0:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                               time.gmtime(time.time() - stale_seconds))
        cur = conn.execute(
            "UPDATE jobs SET status='stalled',error=?,updated_at=?"
            " WHERE status IN ('running','queued') AND updated_at < ?",
            (msg, ts, cutoff))
    else:
        cur = conn.execute(
            "UPDATE jobs SET status='stalled',error=?,updated_at=?"
            " WHERE status IN ('running','queued')", (msg, ts))
    conn.commit()
    return getattr(cur, "rowcount", 0) or 0


def render_key(text_ref, provider, voice, model, speed, extra=""):
    return chunks.cache_key(text_ref, provider, voice, model, speed, extra)


def _chapter_row(conn, book_id, idx):
    r = conn.execute("SELECT * FROM chapters WHERE book_id=? AND idx=?",
                     (book_id, idx)).fetchone()
    if not r:
        raise KeyError("no chapter %s/%s" % (book_id, idx))
    return r


def render_chapter(conn, storage, speech, book_id, idx, voice="", model="", speed=1.0,
                   cred=None, opts=None, voice_label="", pron_rules=None, job_id=None):
    """Render one chapter. Returns "cached" or "rendered".

    Pronunciation fixes are applied to the text here, before the engine sees
    it, so every engine gets them (including the local one that cannot take
    phonemes). The chapter cache key carries a tag for only the rules that
    match this chapter, so a rule elsewhere leaves this chapter's audio alone.

    When the chapter has to be built, it is built from the provider's request
    chunks and each chunk is cached on its own key. A word change that lands in
    one chunk re-synthesises just that chunk and reuses the rest, so the user
    waits for a fraction of the audio, not the whole chapter.
    """
    row = _chapter_row(conn, book_id, idx)
    text = storage.get(row["text_ref"]).decode("utf-8")
    rules = pron_rules or []
    extra = pron.token_for(text, rules)
    key = render_key(row["text_ref"], speech.id, voice, model, speed, extra)
    cached = conn.execute("SELECT * FROM render_cache WHERE `key`=?", (key,)).fetchone()
    if cached:
        _write_chapter_audio(conn, book_id, idx, cached, speech.id, voice, model, speed,
                             voice_label=voice_label, pron_tag=extra)
        conn.commit()
        return "cached"
    o = dict(opts or {})
    o.setdefault("speed", speed)
    o.setdefault("model", model)
    buf = bytearray()
    stop = (lambda: is_cancelled(job_id)) if job_id else None
    for ch in chunks.split(speech, text):
        if stop and stop():
            raise Cancelled()
        ctag = pron.token_for(ch, rules)
        ckey = chunks.cache_key(chunks.cid(ch), speech.id, voice, model, speed, ctag)
        hit = conn.execute("SELECT * FROM render_cache WHERE `key`=?", (ckey,)).fetchone()
        if hit:
            buf += storage.get(hit["audio_ref"])
            continue
        spoken = pron.spoken(ch, rules)
        if not chunks.has_speech(spoken):
            # a scene break is not a sentence: nothing to say, nothing to ask.
            # Sending it anyway is how a strict server kills a whole chapter.
            continue
        audio = _synth(speech, spoken, voice, o, cred, should_stop=stop)
        ref = storage.put(audio.data)
        conn.execute(
            "INSERT OR REPLACE INTO render_cache"
            "(`key`,audio_ref,audio_bytes,duration_s,provider,voice,model,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (ckey, ref, len(audio.data), None, speech.id, voice, model, now()))
        # commit per chunk: a chapter that fails on its last piece used to throw
        # away every chunk it had already spoken, so each retry re-synthesised
        # the whole chapter (and re-billed it, on a metered provider).
        conn.commit()
        buf += audio.data
    data = bytes(buf)
    ref = storage.put(data)
    conn.execute(
        "INSERT OR REPLACE INTO render_cache"
        "(`key`,audio_ref,audio_bytes,duration_s,provider,voice,model,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (key, ref, len(data), None, speech.id, voice, model, now()))
    entry = {"audio_ref": ref, "audio_bytes": len(data), "duration_s": None}
    _write_chapter_audio(conn, book_id, idx, entry, speech.id, voice, model, speed,
                         voice_label=voice_label, pron_tag=extra)
    conn.commit()
    return "rendered"


def stale_chars(conn, storage, text, rules, split, provider, voice, model, speed):
    """How many characters a re-render actually has to synthesise.

    Walks the same chunks the renderer would, and counts only those with no
    cached audio for the current rules. `split` is the provider's chunk
    function; without one, the text is a single unit and the whole thing counts.
    """
    if not callable(split):
        return len(text or "")
    stale = 0
    for ch in split(text):
        ctag = pron.token_for(ch, rules)
        ckey = chunks.cache_key(chunks.cid(ch), provider, voice, model, speed, ctag)
        if conn.execute("SELECT 1 FROM render_cache WHERE `key`=?", (ckey,)).fetchone():
            continue
        stale += len(ch)
    return stale


def stale_book_chars(conn, storage, speech, book_id, idxs, rules, voice, model, speed):
    """Characters a re-render of `idxs` would actually synthesise in this voice.

    Mirrors the renderer chunk for chunk: a chapter already voiced in this exact
    (voice, model, speed, rules) contributes nothing, and a chunk already cached
    contributes nothing. A metered run holds this number before it starts and,
    measured again after, charges only the difference, so re-voicing a cached
    book costs nothing (Chris, 2026-10-21).
    """
    split = (lambda text: chunks.split(speech, text))
    total = 0
    for i in idxs:
        row = _chapter_row(conn, book_id, i)
        if not row:
            continue
        try:
            text = storage.get(row["text_ref"]).decode("utf-8")
        except Exception:
            continue
        total += stale_chars(conn, storage, text, rules, split,
                             speech.id, voice, model, speed)
    return total


def _write_chapter_audio(conn, book_id, idx, entry, provider, voice, model, speed,
                         voice_label="", pron_tag=""):
    conn.execute(
        "UPDATE chapters SET audio_ref=?,audio_bytes=?,duration_s=?,"
        "provider=?,voice=?,model=?,speed=? WHERE book_id=? AND idx=?",
        (entry["audio_ref"], entry["audio_bytes"], entry["duration_s"],
         provider, voice, model, float(speed or 1.0), book_id, idx))
    # keep every voice, not just the newest: the chapter row points at the
    # default, the renditions table is what lets the player offer a choice
    library.add_rendition(conn, book_id, idx, provider, voice, model, speed,
                          entry["audio_ref"], entry["audio_bytes"], entry["duration_s"],
                          voice_label=voice_label, pron=pron_tag)


def start_job(conn, user_id, book_id, provider, voice="", model="", total=0, kind="render",
              status="running"):
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO jobs(id,user_id,book_id,kind,provider,voice,model,status,"
        "total,current,error,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, user_id, book_id, kind, provider, voice, model, status,
         total, 0, "", now(), now()))
    conn.commit()
    return job_id


def _resume_state(conn, book_id, idxs, provider, voice, model, speed, tokens=None):
    """Which of `idxs` are already converted in this exact voice.

    A restart must not throw away an hour of work: chapters that already have
    a rendition for (provider, voice, model, speed) are done, and the render
    resumes at the first one that is not. The pronunciation tag counts too:
    a chapter voiced under a different rule set is not done for the current
    one, so changing a rule re-renders exactly the chapters it affects.
    """
    tokens = tokens or {}
    want = set(idxs)
    have = set()
    for r in library.renditions(conn, book_id):
        if (r["idx"] in want and r["provider"] == provider and r["voice"] == voice
                and (r["model"] or "") == (model or "")
                and abs(float(r["speed"] or 1.0) - float(speed or 1.0)) < 1e-6
                and (r.get("pron") or "") == (tokens.get(r["idx"], ""))):
            have.add(r["idx"])
    chars = 0
    for r in conn.execute("SELECT idx,chars FROM chapters WHERE book_id=?",
                          (book_id,)):
        if r["idx"] in have:
            chars += (r["chars"] or 0)
    return have, chars


def render_book(conn, storage, speech, user_id, book_id, voice="", model="",
                speed=1.0, cred=None, opts=None, only=None, job_id=None,
                voice_label=""):
    """Render all (or `only`) chapters of a book. Returns the job id.

    Resumable: chapters already converted in this voice are counted, not
    redone, so a run restarted after a crash picks up where it stopped. The
    loop runs under a server-wide lock so only one render ever pegs the box.
    """
    rows = conn.execute("SELECT idx FROM chapters WHERE book_id=? ORDER BY idx",
                        (book_id,)).fetchall()
    idxs = [r["idx"] for r in rows]
    if only is not None:
        idxs = [i for i in idxs if i in set(only)]
    # A chapter is already done for this (voice, model, speed) only if its
    # pronunciation tag matches too, so a run restarted after a crash skips
    # what it finished while a changed rule makes just the affected chapters
    # redo. The tag covers only the rules that match each chapter's text.
    rules = pron.for_book(pron.load(conn, user_id), book_id,
                          pron.book_author(conn, book_id),
                          pron.book_series(conn, book_id))
    tokens = {}
    for i in idxs:
        try:
            text = storage.get(_chapter_row(conn, book_id, i)["text_ref"]).decode("utf-8")
        except Exception:
            text = ""
        tokens[i] = pron.token_for(text, rules)
    already, done_chars = _resume_state(conn, book_id, idxs, speech.id, voice,
                                        model, speed, tokens)
    todo = [i for i in idxs if i not in already]
    if job_id is None:
        job_id = start_job(conn, user_id, book_id, speech.id, voice, model, len(idxs))
    if (job(conn, job_id) or {}).get("status") == "cancelled" or is_cancelled(job_id):
        release_cancel(job_id)
        return job_id
    done = len(already)
    conn.execute("UPDATE jobs SET current=?,done_chars=?,updated_at=? WHERE id=?",
                 (done, done_chars, now(), job_id))
    conn.commit()
    with _RENDER_LOCK:
        # A cancel can land while this job waits its turn for the lock, so the
        # status is read again here: a job told to stop never starts.
        if (job(conn, job_id) or {}).get("status") == "cancelled" or is_cancelled(job_id):
            release_cancel(job_id)
            return job_id
        try:
            if os.nice(0) < 10:            # stay a guest on the box, but never
                os.nice(10 - os.nice(0))    # drift past nice 10 across renders
        except Exception:
            pass
        failed = []
        cancelled = False
        try:
            conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?",
                         (now(), job_id))
            conn.commit()
            for i in todo:
                if is_cancelled(job_id):
                    cancelled = True
                    break
                try:
                    render_chapter(conn, storage, speech, book_id, i, voice, model, speed,
                                   cred=cred, opts=opts, voice_label=voice_label,
                                   pron_rules=rules, job_id=job_id)
                except Cancelled:
                    cancelled = True
                    break
                except Exception as e:              # one bad chapter is not the book
                    failed.append((i, "%s: %s" % (type(e).__name__, e)))
                    continue
                done += 1
                cr = conn.execute("SELECT chars FROM chapters WHERE book_id=? AND idx=?",
                                  (book_id, i)).fetchone()
                done_chars += (cr["chars"] if cr and cr["chars"] else 0)
                conn.execute("UPDATE jobs SET current=?,done_chars=?,updated_at=? WHERE id=?",
                             (done, done_chars, now(), job_id))
                conn.commit()
            if cancelled:
                conn.execute("UPDATE jobs SET status='cancelled',error=?,updated_at=?"
                             " WHERE id=?", ("cancelled by user", now(), job_id))
            elif failed:
                chs = ", ".join(str(i + 1) for i, _ in failed)
                why = "; ".join(msg for _, msg in failed)
                conn.execute("UPDATE jobs SET status='error',error=?,updated_at=? WHERE id=?",
                             ("%d chapter(s) failed after retries (ch %s): %s. "
                              "Convert again to resume; the finished chapters are kept."
                              % (len(failed), chs, why), now(), job_id))
            else:
                conn.execute("UPDATE jobs SET status='done',current=?,updated_at=? WHERE id=?",
                             (done, now(), job_id))
        except Exception as e:                      # a failed run must say so, not lie
            conn.execute("UPDATE jobs SET status='error',error=?,updated_at=? WHERE id=?",
                         ("%s: %s" % (type(e).__name__, e), now(), job_id))
        finally:
            release_cancel(job_id)
        conn.commit()
    return job_id


def active_for_book(conn, book_id):
    """True when a render for this book is queued or running. The caller waits:
    deleting a book mid-render would let the worker write rows back for it."""
    r = conn.execute("SELECT 1 FROM jobs WHERE book_id=? AND status IN "
                     "('queued','running') LIMIT 1", (book_id,)).fetchone()
    return bool(r)


def list_for(conn, user_id, limit=20):
    """Recent jobs for one user, newest first. The client shows the active
    ones (queued or running) so a conversion the page forgot is cancellable."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def job(conn, job_id):
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None
