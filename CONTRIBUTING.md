# Contributing to VoiceHive

Thanks for taking a look. VoiceHive is a small project, so the process is short and
informal — there is no CLA to sign and no committee to convince. If something here is
unclear, open an issue and ask.

VoiceHive is at **v0.0.1-beta**, so expect missing pieces and the occasional crash.
"Here's what broke and here's the log" is genuinely useful right now.

## Before you start

- Check the [open issues](https://github.com/xavort/VoiceHive/issues) so we don't end up
  with two people fixing the same thing.
- For anything bigger than a small fix, open an issue first and describe what you want to
  change. It is much easier to agree on a direction before the code exists.

## Setting up

```bat
git clone https://github.com/xavort/VoiceHive
cd VoiceHive
py -3.13 -m pip install -r requirements.txt
py -3.13 main.py
```

The app is plain Tkinter, so there is no build step — edit the file and run it again.

If you want to test the voice pipeline without clicking through the UI, `mini_voice.py`
is built for that:

```bat
py -3.13 mini_voice.py listen     :: join and print the counters
py -3.13 mini_voice.py tone       :: send a 440 Hz tone and check it comes back
py -3.13 mini_voice.py analyze    :: re-check the saved capture
```

It logs the test accounts in, so point it at throwaway tokens.

### Handy switches

| Variable | Effect |
| --- | --- |
| `DISVBC_NO_AUTOSTART=1` | Opens the window without connecting any account. Useful for UI work. |

## What to work on

Good first contributions tend to be:

- A reproducible bug with a clear reproduction — a log line or the status text is gold.
- Voice join failures that report the wrong reason (or no reason).
- Crackling, dropouts or drift in the listen pipeline.
- Anything that makes the UI clearer.

## Code style

There is no formatter config to fight with; just match the file you are editing:

- The UI, the account sessions and the receive pipeline are separate concerns — keep the
  receive code inside `voice_listen.py`.
- Tkinter callbacks must never block. Use timers, `after`, or the existing worker loop
  instead of sleeping or waiting inline.
- Standard library first. The project has exactly one dependency and it would be nice to
  keep it that way; if you need something new, explain why in the PR.
- Prefer a clear variable over a clever one-liner. If a protocol detail is non-obvious
  (and with voice there always is one), leave a comment explaining *why*, not what.

## Please don't commit

- `accounts.json` — it contains real tokens. It is in `.gitignore`; leave it there.
- Tokens, session ids, webhook URLs or full guild/member dumps in test output or logs.
- `__pycache__`, `*.pyc`, `.venv/`, WAV captures or a bundled `ffmpeg.exe`. Run
  `clean.bat` if your tree picked up any of these.

If you accidentally commit a token, treat it as compromised and reset the account's
token in Discord immediately — then read [SECURITY.md](SECURITY.md).

## Sending a pull request

1. Fork the repo and work on a branch.
2. Keep the change focused — one fix or feature per pull request.
3. Say what the change does and *why*, and mention how you tested it. "Ran it on Windows
   11 with three accounts, joined a channel, audio was clean" is a perfectly good test
   note.
4. If the change touches the voice pipeline, a short before/after description goes a long
   way.

Screenshots or a short screen recording are very welcome for UI changes.

## Reporting bugs

Please include:

- what you did, what you expected, what actually happened;
- the status text the app showed (especially for a failed voice join);
- your Windows version and Python version;
- whether the channel was using E2EE/DAVE.

**Do not paste your token**, and blank out any usernames or server names you'd rather
keep private.
