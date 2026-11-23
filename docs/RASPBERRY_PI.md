# Running TradeBot on a Raspberry Pi

A Raspberry Pi 5 can run the daily job, chart alerts and the dashboard around the clock, so the
Mac can sleep. It does everything the Mac does except macOS notification banners (Telegram still
works). A Pi uses about 5 W.

**Only one computer should run the scheduled job.** Two would each update their own copy of the
paper account and could both send Alpaca orders. The steps below move the job from the Mac to the
Pi; the end of this page covers moving it back.

Run each command on the computer named in its step. `<pi-user>` is the user name you choose in
step 1, and `tradebot.local` assumes you name the Pi `tradebot`.

## 1. Set up the Pi (on the Mac)

1. Install **Raspberry Pi Imager** (raspberrypi.com/software) and flash a microSD card (or SSD)
   with **Raspberry Pi OS (64-bit)**. The Lite version is enough; the dashboard is used from a
   browser on another device.
2. In the Imager's settings, set:
   - hostname: `tradebot`
   - a user name and password
   - your Wi-Fi (or plug in Ethernet, which is more reliable)
   - **Enable SSH** (password, or your Mac's public key)
3. Boot the Pi, wait a minute, then connect from the Mac's Terminal:

   ```bash
   ssh <pi-user>@tradebot.local
   ```

   If `tradebot.local` isn't found, use the Pi's IP address from your router's device list.

Use the official 27 W power supply. Under-powered Pis corrupt SD cards.

## 2. Install TradeBot (on the Pi, over SSH)

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-dev build-essential
git clone https://github.com/lifeatpittsburgh-cpu/tradebot.git ~/tradingbot
cd ~/tradingbot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev,dashboard,research,ai]"
pytest -q
```

The install takes several minutes (pandas and friends). All tests should pass.

Raspberry Pi OS ships Python 3.11, so the Pi gets the current Anthropic SDK (1.x); the Mac's
Python 3.9 keeps the last 0.x release. TradeBot works with both.

Add the virtual environment to your shell so `tradebot` works in every SSH session:

```bash
echo 'source ~/tradingbot/.venv/bin/activate' >> ~/.bashrc
```

## 3. Move the job from the Mac (on the Mac)

Stop the Mac's job first, so it can't run while you copy:

```bash
cd ~/Projects/tradingbot && tradebot schedule uninstall
```

Copy your keys and your state (paper account, alert history, chart-alert watchlist, Alpaca
order records, time-zone choice, last AI study):

```bash
scp ~/Projects/tradingbot/.env <pi-user>@tradebot.local:~/tradingbot/.env
```

```bash
rsync -a --exclude logs ~/Projects/tradingbot/state/ <pi-user>@tradebot.local:~/tradingbot/state/
```

`.env` holds your Telegram and Alpaca keys. It goes straight from the Mac to the Pi; never put it
in git or a chat.

## 4. Anthropic key for the AI study (on the Pi)

On the Mac the AI study uses your `ant auth login` sign-in, which isn't in `.env`. On the Pi, the
simplest option is an API key:

1. At console.anthropic.com → **API Keys**, create a key (e.g. named `raspberry-pi`).
2. On the Pi, open `.env` and add the line `ANTHROPIC_API_KEY=` followed by the key:

   ```bash
   nano ~/tradingbot/.env
   ```

## 5. Check everything (on the Pi)

```bash
tradebot alerts test
```

```bash
tradebot broker status
```

```bash
tradebot ai show
```

You should get a Telegram test message, see your Alpaca paper account, and see the AI study
copied from the Mac. `tradebot ai study --refresh` makes a fresh one (one paid request).

## 6. Start the daily job (on the Pi)

```bash
tradebot schedule install
```

```bash
tradebot schedule status
```

This installs a systemd user timer that runs the same 5-minute check as the Mac: chart alerts
every 5 minutes, and the daily run once after 20:15 New York time. The Pi's own time zone doesn't
matter. `install` also turns on *linger*, so the timer keeps running after a reboot with nobody
logged in. If it says it couldn't, run `sudo loginctl enable-linger $USER` once.

Watch a run:

```bash
tradebot schedule run-now && tail -f ~/tradingbot/state/logs/daily.log
```

## 7. Keep the dashboard running (on the Pi)

```bash
tradebot schedule install-dashboard
```

Then open **http://tradebot.local:8501** on the Mac, your phone, or any device on your home
network. The service starts at boot and restarts if it crashes.

The dashboard has **no password**, and its Paper portfolio tab can update or reset the paper
account. Keep it on your home network: never forward port 8501 on your router.

## Day to day

| Task | Command (on the Pi) |
|---|---|
| Is the job running, when's the next run? | `tradebot schedule status` |
| Recent output | `tail -50 ~/tradingbot/state/logs/daily.log` |
| All timers | `systemctl --user list-timers` |
| Dashboard log | `journalctl --user -u tradebot-dashboard -n 50` |
| Restart the dashboard | `systemctl --user restart tradebot-dashboard` |

**Updating to new TradeBot code** (after a PR is merged):

```bash
cd ~/tradingbot && git pull && pip install -e ".[dev,dashboard,research,ai]" && systemctl --user restart tradebot-dashboard
```

The daily job uses the new code at its next 5-minute check.

**Editing settings:** `nano ~/tradingbot/config/settings.yaml`. The job reads it on each run;
restart the dashboard to see changes there.

## Moving the job back to the Mac

On the Pi:

```bash
tradebot schedule uninstall && tradebot schedule uninstall-dashboard
```

On the Mac:

```bash
rsync -a --exclude logs <pi-user>@tradebot.local:~/tradingbot/state/ ~/Projects/tradingbot/state/
```

```bash
cd ~/Projects/tradingbot && tradebot schedule install
```

## Troubleshooting

- **`tradebot.local` doesn't resolve:** run `hostname -I` on the Pi and use that IP instead.
- **The timer stops after logout or reboot:** `loginctl show-user $USER --property=Linger` must
  say `Linger=yes`. Fix it with `sudo loginctl enable-linger $USER`.
- **Wrong times in the log:** the Pi needs the right clock. `timedatectl` should show
  `System clock synchronized: yes`.
- **"AI study unavailable" in alerts:** check `ANTHROPIC_API_KEY` in `.env`, then run
  `tradebot ai study --refresh` to see the error.
