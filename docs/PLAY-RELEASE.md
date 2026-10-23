# Play release: what is built, what is waiting

Status 2026-10-23. The app itself is ready to upload. Everything left is either
paperwork or a Google login.

## Built and verified

- `android-shell/build_aab.sh` produces a signed release bundle:
  `android/app/build/outputs/bundle/release/app-release.aab`, 2,976,467 bytes,
  447 entries, versionCode 2, versionName 1.0.0.
- Signing is injected by `android_src/patch_build_gradle.py` on every build,
  because `npx cap sync` regenerates `android/app/build.gradle`. `build_aab.sh`
  refuses to run without `FS_KEYSTORE`, so an unsigned bundle can't slip out.
- The bundle is signed with the Play upload key, certificate
  `CN=Magic Smoke Technologies LLC, O=Magic Smoke Technologies LLC, C=US`,
  SHA-256 `EE:05:29:6E:84:C4:A7:04:47:E3:2F:FE:50:8A:F5:B6:43:88:12:EE:A4:05:9C:E6:E0:D4:9F:04:EB:49:19:F7`.
  The same key and password live in Key Vault `fireside-kv`
  (`fireside-android-upload-keystore`, `...-password`), and a copy of the
  keystore is at `//10.32.1.6/fieldstone/android-keys/fireside-upload.jks` on
  bench01. Back that up somewhere off this machine before the first upload:
  losing it means the app id is dead.
- targetSdk 35, minSdk 23, package `com.stolworthy.fireside`, no ads, no
  in-app purchases, no billing permission.

## The build, in full

```
export FS_KEYSTORE=/srv/bench/fieldstone/android-keys/fireside-upload.jks
export FS_KS_PASS=<vault: fireside-android-upload-keystore-password>
export FS_KEY_ALIAS=fireside-upload
export FS_VERSION_CODE=2
export FS_VERSION_NAME=1.0.0
./android-shell/build_aab.sh
```

Every upload needs a larger `FS_VERSION_CODE`. Keep it in the commit message.

## Blocked on the D-U-N-S number

An organisation account skips the 12-tester / 14-day closed test that personal
accounts must run. Google matches the legal name and address on the DUNS record
exactly. Use these strings, no suite numbers, no trailing lines:

- Legal name: `Magic Smoke Technologies LLC`
- Address: the exact street address on the LLC registration
- Country: `United States`
- Phone: the number on the DUNS record
- Website: `https://readfireside.com`

If Google's verification says the name does not match, the fix is a DUNS record
correction, not a second Play account.

## Remaining, in order

1. Create the org developer account, pay the one-time $25, accept the
   agreement, complete identity verification.
2. Create the app: name `Fireside`, default language English (US), app (free),
   not a game.
3. Store listing: short description, full description, 512x512 icon, 1024x500
   feature graphic, at least 2 phone screenshots. Icon source is the cream-page
   flame at 512 already in the repo.
4. Data safety form. Draft answers: collects email address and name (account),
   and user-supplied reading material (books, documents) uploaded by the user.
   Data is encrypted in transit. Users can request deletion at
   `https://readfireside.com/delete-account` and in Settings. No data sold, no
   ad SDKs, no third-party analytics beyond what the server logs.
5. Content rating questionnaire: no violence, no user-to-user communication,
   user-generated content is private to the account. Expect "Everyone".
6. Target audience: 18+ (an app that reads books aloud is not child-directed,
   and declaring 13+ triggers Families policy obligations).
7. Upload the `.aab`, opt in to Play App Signing (Google holds the app signing
   key; the upload key above stays ours).
8. Internal testing track first: install on a real device from Play, confirm the
   first-run URL, login, and a book starts narrating.

## Code status before production

- In-app account deletion: DONE, and verified on production, not just in tests.
  `Settings > Account > Delete account` posts to `/account/delete` with the
  account's password (or the phrase `delete my account` for social-only
  accounts). Live check 2026-10-23 18:00Z: registered a throwaway local account,
  called the route, then `/me` returned `authenticated: false` and signing in
  again with the same credentials returned `invalid username or password`. The
  public web path `https://readfireside.com/delete-account` is up (200) and
  names the email fallback for people who uninstalled the app. Play wants both
  halves; both are real.
- Privacy policy URL on the listing: `https://readfireside.com/privacy` (200).

## Business address and registered agent (2026-10-23)

Chris's decision: hire a registered agent and take the home address off every public
business surface. This repo and the hosted site carry no street address today (privacy,
terms and delete-account name only the LLC and fireside@readfireside.com); the address
lives on the SCC filing, the DUNS record, and the Play developer listing.

Order is fixed by the DUNS long pole. Google Play org verification matches the DUNS legal
name and address character for character, so the address must not move before the DUNS
number is issued.

1. DUNS number issued, home address on record.
2. Create the Play org account with that exact address.
3. Hire the registered agent; file the VA Statement of Change of Registered Office /
   Registered Agent (no filing fee).
4. Update the DUNS record to the agent address, and tell Google the new address.
5. Change the developer address in Play Console before the first release. The listing
   contact address is not published until an app is live.

Constraints that bite:
- Play wants a street address. No PO boxes, and a bare virtual office is rejected. The
  agent must supply a usable business address, not only a registered office.
- Mail forwarding must be enabled: state annual report notices and any IRS letter go to
  the registered office now, not to the house.
- Open the business bank account before the change if it is not open yet; KYC after an
  address move is avoidable pain. Bank and Stripe addresses are not public surfaces.

## Store listing copy (paste-in, written in Ren's voice, 10-23)

App name: `Fireside`
Category: Books & Reference
Contact email: `fireside@readfireside.com`
Privacy: `https://readfireside.com/privacy`
Icon: `assets/icon-512.png` (flame as-is, confirmed by Chris 10-23). Keep
`icon-maskable-512.png` for adaptive.

### Short description (80 char limit)

```
Drop in an ebook or a story link. It reads it aloud and keeps going.
```

### Full description (4000 char limit)

```
Fireside reads books out loud, in a voice you pick, and keeps going when you put the phone down.

ADD SOMETHING TO READ
Drop in an EPUB, DOCX, or PDF, or paste a link to a public story. Fireside splits it into chapters itself, so you never sit there picking page breaks. Scene breaks, headings, a chapter that is one long paragraph, it handles them, and it tells you when a chapter is done.

ONE VOICE PER BOOK
Choose a voice for each book and it remembers. Same book, same voice, replayed as often as you like, costs nothing more.

PLAYBACK THAT SURVIVES
Lock screen, notification shade, a walk to the shop. It stays running and the notification tells you what it is doing.

YOU BUY READING BY THE CHARACTER
Credits are bought once and never expire. A book you already own in a voice you already paid for is free to replay forever. A different voice for the same book is new audio, so that is charged. A monthly plan covers hosting and storage; narration is separate. Nothing is sold inside this app: your account and your credits live on readfireside.com in a browser, and this app signs into that account.

BRING YOUR OWN VOICE SERVER
If you already run a text to speech server you like, point Fireside at it and skip the narration charge.

WHAT IT DOES NOT DO YET
English voices only. A scanned PDF with no text layer is refused instead of read out as nonsense. No shelves of extra features.

WHO MAKES IT
Magic Smoke Technologies LLC, a small company in Richmond, Virginia. Fireside is built by a human and an AI agent working together; we say so in the app, because it is true and it is unusual.

Your library is yours. No ad SDK, no analytics SDK, nothing sold, nothing shared. Delete your account and everything goes with it. Privacy policy: readfireside.com/privacy
```

### Still to produce before submission

- 1024x500 feature graphic (flame on the cream page, one line of copy).
- At least 2 phone screenshots from the release build: library screen, and a
  book mid-narration with the lock-screen notification visible.
- Data safety + content rating answers: already drafted in "Remaining, in order"
  above; they do not change for this copy.
