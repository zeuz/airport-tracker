# get-traffic.py

Per-airport arrivals log with **hourly** counts (data from [OpenSky Network](https://opensky-network.org)) and **MRTG-style** bar graphs rendered as HTML.

## Installation

The project uses the `./venv` virtualenv (Python 3.14):

```bash
uv pip install --python venv/bin/python -r requirements.txt
cp get-traffic.ini.example get-traffic.ini     # then fill in client_id / client_secret (optional)
```

`get-traffic.ini` holds your OpenSky credentials, so it is in `.gitignore`. Only `get-traffic.ini.example`, which has empty credentials, goes into git.

## Files

| File | Purpose |
|---|---|
| `get-traffic.py` | Main program |
| `get-traffic.ini` | Configuration (paths, OpenSky credentials, mode, colors) |
| `airports.csv` | Airports list (see format below) |
| `data/traffic-log.csv` | Generated log: `hour,icao,count,source,checked_at` |
| `gen-rep.sh` | Hourly job: check + report + publish (see below) |
| `publish.sh`, `publish.conf` | Publishes the report to GitHub Pages (see below) |
| `styles.css` | Dark theme for the report (`[graph] css-html`) |

## Airports list format

Columns are accepted in English or Spanish, in any order:

| Field | Accepted columns | Required |
|---|---|---|
| ICAO code | `icao`, `designacion_icao` | yes |
| Name | `name`, `nombre_aeropuerto` | no |
| IATA | `iata`, `designacion_iata` | no |
| Latitude / longitude | `lat`/`lon`, `latitude`/`longitude`, `latitud`/`longitud` | yes, for approach mode |
| Time zone | `timezone`, `zona_horaria` | no (falls back to the `.ini` one) |

Any other column (e.g. `tipo_complejo`, `ciudad_cercana`, `ciudad_grande`) is shown as information in each airport's header in the report.

## Usage

```bash
# Inbound flights right now (approach mode) -> recorded in the current hour
venv/bin/python get-traffic.py --check-traffic

# Actual historical arrivals per hour over a range (requires OpenSky credentials)
venv/bin/python get-traffic.py --check-traffic --from-date 01-11-2026 --to-date 02-11-2026

# MRTG-style HTML report (default: last 24 h)
venv/bin/python get-traffic.py --generate-graph report.html
venv/bin/python get-traffic.py --generate-graph report.html --from-date 01-11-2026 --to-date 07-11-2026

# Both actions at once
venv/bin/python get-traffic.py --check-traffic --generate-graph report.html
```

- `--to-date` includes that whole day. If only one of the two dates is given, the range is that day.
- The first graph in the report is the **total of inbound flights**, the sum over all airports in the `[general] timezone` time zone. Then comes one section per airport.
- If the range is longer than 2 days, a daily graph ("Weekly") is added below the hourly one.
- **Next to each hourly graph** there is a **this week** graph, between the hourly and the weekly graphs: one bar per day of the current week, **Sunday to Saturday**, labeled `S M T W T F S`.
  - "This week" is the week that contains the report's last day (today, for the default report). Days that haven't happened yet have no bar.
  - Each bar is the sum of that day's counts; hovering shows the day and date. Below it there is a MAX / AVG / NOW / TOTAL table per day.
  - Its width is set with `this_week_width` in `[graph]` (210 by default).
- **To the right**, there is a **weekly total** graph (Monday to Sunday) for the last `weeks` weeks (8 by default), ending with the week of the report's last day.
  - It uses the whole log history up to that date, not just the requested range.
  - Below it there is a MAX / AVG / NOW / TOTAL table per week. The current week shows as incomplete.
  - It is tuned with `weeks` and `week_width` in `[graph]`.
- **The three graphs (hourly, this week, weekly) always stay on the same row.** Each one takes a share of the row proportional to its width (`width`, `this_week_width`, `week_width`), and on narrower windows all three scale down together instead of wrapping.
- **Custom header:** with `info-html = info.html` in the `[graph]` section of the `.ini`, that file's content replaces the report title. The path is relative to the `.ini`. It can be a fragment (`<h1>…</h1><p>…</p>`) or a full page; from a full page, `<body>` and its `<style>` blocks are used. The HTML is inserted as is. If the value is empty, `title` is used; if the file is missing, the program reports it before querying OpenSky.
- **Custom styles:** with `css-html = styles.css` in the `[graph]` section, that stylesheet is embedded in the report after the built-in styles, so it can override them. `<style>` tags in the file are stripped. If the value is empty, nothing is added; if the file is missing, a warning is shown and the report is generated without those styles.
  - If the stylesheet defines these variables in `:root`, the report's graphs, headings and tables use those colors: `--bg-color`, `--card-bg`, `--text-main`, `--text-muted`, `--border-color`, `--accent` and `--row-alt`.
  - The bundled `styles.css` is a dark theme that defines those variables.
  - The whole report sits inside a `<div class="container report">`, so rules like `body { display:flex }` don't break the layout.
- Bar colors come from `bar_color` and `max_bar_color` (the tallest bar of each graph) in `[graph]`.
- A new measurement for the same hour/airport/source replaces the previous one in the log.

## Counting modes (`[check] mode`)

- **approach** (default, no dates): counts aircraft in flight, descending, and below `max_altitude_m` within `approach_radius_km` of the airport. It is the real-time estimate of "inbound flights". Works without credentials.
- **arrivals** (always used when dates are given): uses `/flights/arrival` and groups by local landing hour (`lastSeen`).

### OpenSky limitations

- `/flights/arrival` is **historical**: OpenSky processes it in batches (usually the following night), so it is not useful for the current day.
- That endpoint **requires an account**: without credentials it returns HTTP 403. Create an *API client* in your OpenSky account and fill in `client_id` / `client_secret` in `get-traffic.ini`.
- There are daily quota limits; `request_delay` controls the pause between requests.

## Tests

Tests live in `tests/`, use pytest and need no network: OpenSky is mocked.

```bash
uv pip install --python venv/bin/python -r requirements-dev.txt
venv/bin/python -m pytest
```

## Hourly runs (`gen-rep.sh`)

`gen-rep.sh` runs `--check-traffic --generate-graph report.html` and appends the output to `data/gen-rep.log`. It uses a lock (`flock`) so runs never overlap and keeps only the last 5000 lines of the log. It optionally takes another report name: `./gen-rep.sh other.html`.

On WSL there are two ways to schedule it. **Use only one of them.**

**A) Windows Task Scheduler.** Starts WSL even when it is closed. In PowerShell:

```powershell
wsl -l -v    # confirm the distro name (e.g. kali-linux)
schtasks /Create /TN "get-traffic" /SC HOURLY /ST 00:05 /F /TR "wsl.exe -d kali-linux -u zeuz -- /mnt/c/Data-docker/vibe/track-airport-bussine/gen-rep.sh"
schtasks /Run /TN "get-traffic"     # immediate test
```

**B) cron inside WSL.** Only runs while WSL is up, and WSL shuts down by itself when no terminals are open. Use either your user crontab or `/etc/crontab`:

```bash
crontab -e
# add:
5 * * * * /mnt/c/Data-docker/vibe/track-airport-bussine/gen-rep.sh
```

In `/etc/crontab` the line takes the user before the command:
```
5  *    * * *   zeuz    /mnt/c/Data-docker/vibe/track-airport-bussine/gen-rep.sh
```

## Publishing to GitHub Pages (`publish.sh`)

After each successful run, `gen-rep.sh` calls `publish.sh`. It copies the report to `<REPO_DIR>/<SUBDIR>/index.html` in a local clone of the site repository, then commits and pushes it. The site clone lives in the project's `output/` folder, so the report ends up in `output/traffic/index.html` and is served at **https://zeuz.github.io/traffic/**. The rest of the site isn't touched.

Publishing is configured only in `publish.conf`, not in the program or in `get-traffic.ini`:

```bash
ENABLED=false               # set to true when the steps below are done
REPO_DIR=output             # local clone of the site (relative = relative to the project folder)
SUBDIR=traffic              # folder inside the site; empty = site root (replaces the main index.html)
BRANCH=main
REMOTE=origin
```

Pages sites are **public**, even when the repository is private.

**One-time setup:**
1. Configure git and an SSH key for GitHub. cron has no terminal, so the key must have **no passphrase**.
   ```bash
   git config --global user.name "Your Name"
   git config --global user.email "you@example.com"
   ssh-keygen -t ed25519 -C "you@example.com" -N ""      # -N "" = no passphrase
   cat ~/.ssh/id_ed25519.pub                             # add it at GitHub > Settings > SSH and GPG keys
   ssh -T git@github.com                                 # answer "yes" once; must say "Hi <user>!"
   ```
2. Clone the site into `REPO_DIR` (already done: `output/`).
   ```bash
   git clone git@github.com:zeuz/zeuz.github.io.git output
   git -C output config core.fileMode false   # avoids false changes on /mnt/c
   ```
3. Set `ENABLED=true` in `publish.conf` and try it by hand:
   ```bash
   ./publish.sh && tail -n 3 data/gen-rep.log
   ```
   It should print `[publish] published traffic/index.html to origin/main` (or `nothing to publish` if the report hasn't changed). GitHub Pages usually takes a minute or two to show the change.

**Behavior:**
- If the report didn't change, there is no commit (`nothing to publish`).
- Before copying, it pulls the site (`git pull --rebase --autostash`), so edits made on github.com are kept.
- It never waits for a password or prompt (`BatchMode=yes`). If authentication fails, it logs `ERROR` and exits with 1 instead of hanging cron.
- `data/gen-rep.log` records the output and a `Publish (exit code N)` line after each `End`. Publishing only runs if the check and report finished with exit code 0.
- Each publish is one commit in the site repository, so roughly 24 per day.

### Checking that cron is working

Run these steps from the project folder. The job runs at minute 5 of every hour, so wait until at least one `hh:05` has passed.

**1. The job is registered:**
```bash
crontab -l | grep gen-rep           # if you added it with crontab -e
grep gen-rep /etc/crontab           # if you added it to /etc/crontab
```
Put it in **only one** of the two places; if it is in both, it runs twice. After editing `/etc/crontab`, cron reloads it on its own within a minute. Confirm with `sudo journalctl -u cron | grep RELOAD`.

**2. The cron service is active:**
```bash
systemctl is-active cron        # should answer: active
```
If it answers `inactive`, enable it with `sudo systemctl enable --now cron`.

**3. cron launched the script.** Look for the `CMD` line with your user:
```bash
sudo journalctl -u cron --since today --no-pager | grep gen-rep
# e.g.: Nov 06 18:05:01 ... CRON[1234]: (zeuz) CMD (/mnt/c/Data-docker/vibe/track-airport-bussine/gen-rep.sh)
```
Without `sudo`, `journalctl` only shows everything if your user is in the `systemd-journal` group. To avoid `sudo`: `sudo usermod -aG systemd-journal $USER`, then close and reopen the WSL terminal.

**4. The script finished cleanly.** Each run leaves a `Start` and an `End (exit code 0)` line with the time:
```bash
tail -n 20 data/gen-rep.log
grep -E 'Start|End' data/gen-rep.log | tail -n 6      # only the latest runs
```
- `End (exit code 1)` means there was an error; the lines above it say which one (credentials, network, etc.).
- `WARNING: a run is already in progress` means the previous run was still going. It's normal if it happens now and then.

**5. The log and the report are updated every hour:**
```bash
tail -n 7 data/traffic-log.csv        # the last column (checked_at) should be from the last hour
ls -l --time-style=+'%F %T' report.html
```

**Quick all-in-one check:**
```bash
echo "cron: $(systemctl is-active cron)"; cat <(crontab -l 2>/dev/null) /etc/crontab | grep -v '^#' | grep -c gen-rep | sed 's/^/jobs: /'; \
sudo journalctl -q -u cron --since today --no-pager | grep -c 'CMD (.*gen-rep' | sed 's/^/runs today: /'; \
grep -E 'Start|End' data/gen-rep.log | tail -n 2
```

**Troubleshooting:**

| Symptom | Likely cause | What to do |
|---|---|---|
| No `CMD` line in `journalctl` | WSL was off at that time, or cron is inactive | Check step 2. From PowerShell, `wsl -l -v` should show the distro as `Running`. WSL shuts down when you close all its terminals; to avoid depending on that, use option A. |
| `CMD` shows up but there is no `Start` in the log | The script could not start (permissions or path) | `ls -l gen-rep.sh` must show `x`; run it by hand: `./gen-rep.sh` |
| `End (exit code 1)` | Program error | Read the preceding lines in `data/gen-rep.log` |
| `Publish (exit code 1)` | Git/SSH problem when publishing | Read the `[publish] ERROR` line above it. Run `./publish.sh` by hand and `ssh -T git@github.com` to check the key |
| The log time doesn't match yours | WSL time zone | `date` and `timedatectl` show the zone; cron uses the system one |

To test without waiting for minute 5, run the script by hand: `./gen-rep.sh && tail -n 3 data/gen-rep.log`.
