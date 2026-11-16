"""Read one member of an uploaded zip without trusting its size.

An EPUB or a DOCX is a zip the user supplied, and a few kilobytes of it can
inflate to gigabytes. The upload cap bounds the compressed bytes only, so every
member read goes through here: it stops at a limit instead of at the end of the
stream, whatever the header claims the size is.
"""

import zipfile

MEMBER_LIMIT = 64 * 1024 * 1024     # a chapter or a document body, with room to spare


def read(z, name, limit=None):
    """z.read(name), or BadZipFile when the member inflates past `limit`.

    BadZipFile is what every reader already treats as "not a usable file", so a
    bomb is refused the same way a corrupt archive is.
    """
    limit = MEMBER_LIMIT if limit is None else limit
    with z.open(name) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise zipfile.BadZipFile("%s is larger than %d bytes once unpacked"
                                 % (name, limit))
    return data
