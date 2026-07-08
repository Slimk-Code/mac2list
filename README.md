# Mac2List

A CLI tool to convert your own Stalker/MAG-portal IPTV subscription into a standard M3U playlist — so you can use it in any lightweight player instead of a heavy provider app.

![status](https://img.shields.io/badge/status-active-brightgreen) ![python](https://img.shields.io/badge/python-3.8%2B-blue) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

> ⚠️ **Intended use**: This tool is meant only for converting a Stalker/MAG-portal IPTV subscription that you own or are otherwise authorized to access. It authenticates using your own MAC/portal credentials — it does not generate, guess, or brute-force credentials for portals you don't have access to. Use it responsibly and only with accounts you're authorized on.

## Why Mac2List?

Most IPTV portal providers ship their own app to play your channels — and a lot of those apps are heavy, slow, or just unpleasant to use on a phone. Mac2List lets you take the channel/category data from your own portal subscription and convert it into a standard M3U playlist, so you can load it straight into any player you actually like (VLC, IPTV Smarters, Kodi, etc.) without running the provider's bloated app.

## Features

- 🔑 **Portal handshake** — authenticates with your Stalker/MAG portal using your own MAC address and portal URL
- 📺 **Category & channel fetching** — pulls your subscription's full channel list and categories
- 🔄 **M3U conversion** — converts fetched channel data into a standard, player-ready `.m3u` playlist
- ⚡ **Batch fetching** — retrieves categories/channels in parallel for faster extraction
- 🖥️ **CLI-based** — run it from the terminal, no GUI or browser required
- 📴 **Local & offline output** — the resulting playlist is a plain file you keep and use locally, no third-party server involved

## Getting Started

### Requirements

- Python 3.8+
- `requests` library

### Installation

```bash
git clone https://github.com/<your-username>/mac2list.git
cd mac2list
pip install requests
```

### Usage

```bash
python mac2list.py --url http://your-portal-url --mac 00:1A:79:XX:XX:XX
```

This will:
1. Perform a handshake with your portal using the provided MAC address
2. Fetch your available categories and channels
3. Convert the result into an `.m3u` playlist file you can load into any IPTV player

Replace the URL and MAC address with the ones provided by your own IPTV subscription.

## Output

The generated `.m3u` file follows the standard M3U/M3U8 playlist format and can be imported directly into:

- VLC
- IPTV Smarters
- Kodi (via PVR IPTV Simple Client)
- Most other IPTV-capable media players

## Disclaimer

This tool is provided for personal use with IPTV subscriptions you legitimately own or are authorized to access. The author is not responsible for misuse of this tool against portals or accounts you do not have authorization to use. Always comply with your provider's terms of service.

## License

MIT — free to use, modify, and distribute.
