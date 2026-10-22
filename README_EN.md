# Libby's Test Server Monitor

A Windows 11 desktop app that checks the Garry's Mod server **Libby's Test Server** at `103.170.155.169:27017` using the Source UDP A2S_INFO and A2S_PLAYER protocols.

A ready-to-run Windows executable is included at `dist\LibbysServerMonitor.exe`. This same folder includes both Portuguese and English: Portuguese is the default, and you can switch languages in the app's **Customization** tab without keeping separate folders.

## Features

- Checks the server every 10 seconds by default. Without developer mode, the interval can be set from 5 to 300 seconds; with developer mode enabled, it can be set from 1 second up to Python's supported Windows wait limit.
- Handles A2S_INFO challenges and Source responses split across multiple packets.
- Makes up to three UDP attempts per check. It only marks the server as unresponsive after three consecutive check cycles without a valid response.
- Confirms that the server is back online after two consecutive valid responses.
- Avoids sending catch-up checks in a burst when a query takes longer than the configured interval. The player list is queried no more than once every 10 seconds, and the latest list stays visible through brief network failures.
- Shows the map, server version, and time of the most recent check.
- The **Players** tab shows player names, scores, and connection time shared by the server through A2S_PLAYER.
- The **Bad Players** tab keeps an editable personal list and highlights matching names in the Players tab.
- The **Console** tab follows new lines in Garry's Mod's console log inside the app window.
- The **History** tab records confirmed changes between online and unresponsive. It stores up to 500 events on this computer and lets you choose which changes to record or disable new recording.
- **Developer mode** is off by default in the **Customization** tab and unlocks advanced options. When it is on, check **Enable developer console** to show the **Developer** tab, which keeps up to 3,000 recent events without chat message text.
- The **Customization** tab lets you choose the server name, IPv4 address, query port, check interval, console colors, font, text size, word wrapping, and automatic scrolling.
- Starts automatically when the current Windows user signs in by default. You can turn this option off in the app.
- Shows the A2S response time in milliseconds after each successful check.

A missing response can be caused by packet loss, routing, or network filtering. The app reports **Unresponsive** after several confirmation checks; it cannot prove that the server process crashed.

## Run the app from source

1. Install Python 3.10 or newer for Windows. Enable **Add python.exe to PATH** during installation.
2. Copy or extract the `LibbysServerMonitor` folder somewhere you want to keep it.
3. Double-click `run.bat`, or run `python app.py` from this folder. The app only uses components included with Python.

The app tries to enable automatic startup for the current user under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. You can change this setting in the app. Administrator privileges are not required.

## Use the Console tab

Garry's Mod can save console output to a file such as `console.txt` with `-condebug`; some configurations use a `.log` file.

1. In Steam, open **Library**, right-click **Garry's Mod**, and choose **Properties**.
2. Under **General > Launch Options**, add `-condebug`.
3. If the game is already open, close and relaunch it to apply the option.
4. Open this app's **Console** tab and click **Select log file**. Choose the file your game generates, such as `console.log` or `console.txt`, normally in the game's `garrysmod` folder.

The app follows that file in real time and remembers your selection. The view is limited to the most recent 5,000 lines to keep the window responsive. This log comes from your local game client and may include messages it receives while playing. It does not provide access to a dedicated server's private console or other players' consoles. By default, `-condebug` writes to `console.txt`, as described in the [official Garry's Mod wiki](https://wiki.facepunch.com/gmod/ConVars_In_Garrysmod).

## Optional addon for logging messages to your local console

Public A2S queries do not contain chat messages. The included client addon is optional and copies messages received by your own client into its local console:

1. Close Garry's Mod.
2. Copy `gmod_addon\libbys_server_monitor_chat` into the game's `garrysmod\addons` folder. The final path should include `garrysmod\addons\libbys_server_monitor_chat\lua\autorun\client\libbys_server_monitor_chat.lua`.
3. Enable console logging in Garry's Mod's Steam launch options and start the game.
4. Join the monitored server. The addon copies received messages into the local console, which are written to the console log file.
5. In the app's **Console** tab, select the Garry's Mod log file, such as `console.log` or `console.txt`, to see those lines with the rest of the console.

The addon prints a line tagged `[LSM_CHAT]` for each message received. The app no longer has a separate **Chat** tab or filters these lines; they appear as part of the local log in the **Console** tab. This does not provide access to the server's private console or messages your client did not receive.

The addon records only messages received by your client while connected; the app filters entries by the IP address and port configured in the monitor. Messages from before installation, private messages, or messages not delivered to your client will not appear. A complete server-side log, including messages from when you are disconnected, would require a logger installed on the server and permission from its administrator. The log stays on your computer and may contain conversations, so consider people's privacy before sharing it.

## Customize the app

In the **Customization** tab, choose **Portuguese** or **English** in the **Language** section. Portuguese is the default. After you click **Apply and save**, the app restarts automatically in the selected language. You can also enter the server name, IPv4 address, and query port. The default port is `27017`. Click **Apply and save** to start checking the new address. Without developer mode, the interval can be set from 5 to 300 seconds. When developer mode is enabled, the minimum becomes 1 second and the maximum follows the wait limit supported by Python on Windows. Select **Enable developer mode** to unlock the expanded interval range. Also select **Enable developer console** to show or hide the **Developer** tab. For the console, choose an available monospaced font, a size from 8 to 20, background and text colors, word wrapping, and automatic scrolling. The tab records tab changes, A2S queries, configuration changes, and updates to followed files. Your settings are saved on this computer. The selected server address and interval also appear in the **Monitor** tab.

In **Customization**, use **Enable history** to stop or resume recording new changes. Under **Filter history**, independently choose whether to record when the server becomes unavailable and when it comes back online; leave both selected to record everything. These settings affect new entries only and preserve older ones. In **History**, view the latest 500 changes or remove them with **Clear history**.

In **Bad Players**, enter a name to add it to your personal list. Select an entry and click **Remove selected** to delete it. Names are saved on this computer. This does not ban anyone from the server; it only highlights matching names in your local list. Scores are read as whole numbers, and the **Index** column is the A2S index sent by the server, not a SteamID.

## Source files

Both languages are included in one folder. The English interface text is in `app_en.py`, the Portuguese text is in `app_pt.py`, and `app.py` opens the language saved in the app's settings. If you edit the wording, change the matching source file, then run `run.bat` to open the source version. To rebuild the executable with both languages, run `build_windows.bat`.

## Build a Windows executable

On a Windows computer with Python 3.10 or newer and internet access:

1. Double-click `build_windows.bat`.
2. The script installs PyInstaller and creates `dist\LibbysServerMonitor.exe` with both languages.
3. Copy the executable to a location where you want to keep it and run it. You can then enable automatic startup from the app.

The executable is built on Windows because the packaging tool creates a platform-specific binary. The source code remains available alongside this guide.

## Notes

- The app must stay open to keep monitoring. Closing the window stops checks; minimizing it keeps the monitor running.
- Your network and firewall must allow outgoing UDP traffic to the server's query address and the corresponding replies.
- To monitor another server, enter its IPv4 address and A2S query port in the **Customization** tab.
- Some servers do not respond to A2S_PLAYER or hide their player list. In that case, the **Players** tab reports that it could not refresh the list; this does not change the monitor's availability status.
- History only records confirmed state changes; a single missed packet does not create an event.
- The monitor only queries public A2S_INFO and A2S_PLAYER information. It does not join the game, authenticate, ban players, or change the server.
- A2S response time measures the full successful query, including challenges and retries. It is not an ICMP ping measurement.
- Challenge handling follows the protocol description published by [Valve](https://steamcommunity.com/discussions/forum/14/2974028351344359625/).

## Run the protocol checks

Run `python -m unittest discover -s tests -v` to validate A2S_INFO and A2S_PLAYER responses, UDP challenges, split responses, and confirmed state changes without relying on the live server.
