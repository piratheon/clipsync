# ClipSync

Sync plain-text clipboard content between an Android device and a Linux or macOS computer over ADB. Use it for one-off transfers or keep both clipboards synchronized in the background.

## Features

- Two-way clipboard sync, with a configurable polling interval.
- Send text to Android or pull text from Android on demand.
- Automatically tries available Android clipboard backends: `cmd`, `service`, then the optional Clipper helper app.
- Device selection for setups with multiple ADB devices.
- Size and NUL-byte checks to avoid sending unsupported payloads.
- Clipboard text is not written to logs; logs report lengths instead.

## Requirements

- Python 3.10 or newer.
- Android Platform Tools (`adb`).
- USB debugging enabled on the Android device, with this computer authorized.
- A desktop clipboard command:
  - **Linux / Wayland:** `wl-clipboard` (`wl-copy` and `wl-paste`).
  - **Linux / X11:** `xclip` or `xsel`.
  - **macOS:** built-in `pbcopy` and `pbpaste`.

No Python packages need to be installed.

## Install

1. Download `clipsync.py` from this repository.
2. Install ADB and the desktop clipboard utility for your environment.
3. Connect your phone over USB, enable **USB debugging**, and accept the authorization prompt.
4. Confirm that ADB can see the phone:

   ```sh
   adb devices
   ```

5. Check which clipboard backend works:

   ```sh
   python3 clipsync.py check
   ```

For the strongest initial check, copy some text on the phone and run `python3 clipsync.py check --roundtrip`. This writes a test token to the phone clipboard, so it replaces the current phone clipboard contents.

## Usage

Run commands from the directory containing `clipsync.py`.

| Command | What it does |
| --- | --- |
| `python3 clipsync.py check` | Check ADB, the PC clipboard, and Android clipboard backends. |
| `python3 clipsync.py push` | Copy the PC clipboard to the phone. |
| `python3 clipsync.py push --text "hello"` | Send literal text to the phone. |
| `python3 clipsync.py push --stdin < message.txt` | Send text read from standard input. |
| `python3 clipsync.py pull` | Copy the phone clipboard to the PC. |
| `python3 clipsync.py pull --stdout` | Write the phone clipboard to standard output. |
| `python3 clipsync.py daemon` | Keep both clipboards in sync until stopped. |

Stop the daemon with **Ctrl+C**. It polls every second by default.

### Options

Options go after the command name. For example:

```sh
python3 clipsync.py daemon --interval 0.5
python3 clipsync.py check --serial YOUR_DEVICE_SERIAL
python3 clipsync.py push --phone-backend service --text "hello"
```

- `--serial SERIAL` / `-s SERIAL`: choose an ADB device. If multiple devices are connected, selection is required.
- `--phone-backend auto|cmd|service|clipper`: choose a phone clipboard backend. `auto` is the default.
- `--max-bytes N`: maximum text size; defaults to 65,536 bytes. The `service` backend has a 24 KiB limit.
- `--interval SECONDS`: daemon polling interval; minimum 0.2 seconds.
- `--verbose`: enable debug logging.
- `check --roundtrip`: test writing and reading by replacing the phone clipboard with a temporary token.
- `check --show-raw`: print the raw `cmd clipboard get-text` response. This can reveal clipboard contents; use with care.

## Android clipboard backends

ClipSync tries these backends in order when `--phone-backend auto` is used:

1. **`cmd`** — uses `cmd clipboard`. Available on some Android builds.
2. **`service`** — calls the clipboard binder service directly without needing a helper app. Its transaction IDs and parcel layout were verified on Android 16 (SDK 36); other Android versions may differ.
3. **`clipper`** — uses the optional [Clipper helper app](https://github.com/majido/clipper/releases/download/v1.2.1/clipper.apk) (package `ca.zgrs.clipper`). Install it, open it once, and allow it to run in the background. See the [source and releases](https://github.com/majido/clipper/releases).

If no backend works, run `python3 clipsync.py check --verbose` for diagnostics. Backend availability depends on the Android build and its clipboard restrictions.

## Notes and privacy

- Only plain text is supported; images, files, and rich clipboard objects are not synced.
- Empty text, NUL bytes, and text above the configured size limit are skipped or rejected.
- If both clipboards change between polls, the PC value wins.
- The script does not intentionally log clipboard contents. Do not use `check --show-raw` with sensitive clipboard data.
- USB debugging gives the authorized computer ADB-level access to the device. Disable it when you no longer need it.

## License

[MIT](LICENSE)
