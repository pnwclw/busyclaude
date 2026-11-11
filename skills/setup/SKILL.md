---
name: setup
description: Put the Claude Code app on a BUSY Bar and check that it works. Run once after installing the plugin, and again after updating it.
disable-model-invocation: true
---

Set up the user's BUSY Bar for this plugin.

1. First tell the user, in one line, that the bar will ask them to press START at the end: the command blocks while it waits, so saying it afterwards is too late. Then run this with the Bash tool, with a timeout of 3 minutes. Pass along any arguments the user gave after the command (`$ARGUMENTS`), such as `--addr 192.168.1.50` for a bar on Wi-Fi:

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/busyclaude.py" setup --plugin $ARGUMENTS
   ```

2. Tell the user what it printed, briefly and in their language. On success it says which button came back, which is what proves the bar it found is the one in front of them; say so, and that the hooks start working in the next Claude Code session. If it says no button came back, the app is installed and the hooks are in place, but the bar showing the mascot may not be theirs: `status` says which address it is using.

3. If it failed, say why and what to do, from its message:
   - "no BUSY Bar at ...": the bar is not plugged in over USB, or it is on Wi-Fi and needs `/busyclaude:setup --addr <the bar's IP>`.
   - "refused": the bar wants its HTTP API key over Wi-Fi. The user finds it in the bar's web UI under Network > HTTP API. Ask them to run `BUSYBAR_TOKEN=<key> python3 "${CLAUDE_PLUGIN_ROOT}/busyclaude.py" setup --plugin --addr <IP>` in their own terminal, so the key is not pasted into the chat.
   - "the app needs HTTP API ...": the bar's firmware is older than 1.3.1; it has to be updated first.
   - `python3` not found: Python 3 is not installed. On a Mac, `xcode-select --install` provides it.

Other things the same script does, if the user asks: `test perm` shows a sample prompt, `demo` walks through every kind of notification, `status` says what is set up, `uninstall --app` removes the app from the bar.
