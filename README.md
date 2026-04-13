# HP Gas Booking Bot

Automates booking HP Gas LPG refill cylinders via the myhpgas.in chatbot.
Reads customer phone numbers from an Excel file, writes 6-digit delivery
confirmation codes back to the same file.

See `docs/superpowers/specs/2026-04-13-hp-gas-booking-bot-design.md` for the
full design.

## Setup

Windows, Python 3.12:

```
pip install -r requirements.txt
python -m playwright install chromium
```

Edit `booking_bot/config.py` and set `OPERATOR_PHONE` to your own 10-digit
number (the one registered with HP Gas for OTP auth).

## Run

1. Drop `file1.xlsx` in `Input/`. Column A = consumer number (untouched),
   Column B = customer phone number. No header row.
2. Start the bot:
   ```
   python -m booking_bot Input/file1.xlsx
   ```
3. When prompted, enter the OTP you receive on `OPERATOR_PHONE`.
4. The bot will iterate through pending rows (where column C is empty),
   booking each one. Column C is filled with the 6-digit code on success or
   `ISSUE` on failure.
5. Failures are written to `Issues/file1.xlsx` with the full chatbot text so
   you can inspect them.
6. Real-time logs live in `logs/booking_bot_<timestamp>.log`. Tail them in a
   second terminal:
   ```
   Get-Content -Path .\logs\booking_bot_*.log -Wait   # PowerShell
   ```

## Resume

If the bot crashes or you Ctrl-C it, just run it again with the same input
file. Rows that already have a value in column C (code or `ISSUE`) are
skipped; only pending rows are attempted.

## Tests

```
pytest                                    # offline Tier-1 unit tests
$env:BOOKING_BOT_SMOKE="1"; pytest tests/test_smoke.py   # live Tier-2 smoke
```

## Directory layout

| Dir | Purpose |
|---|---|
| `Input/`  | Drop input .xlsx files here. Never modified by the bot. |
| `Output/` | Bot writes the mirror + column C here. |
| `Issues/` | Bot appends one row per failure with diagnostic text. |
| `logs/`   | One log file per run. |
