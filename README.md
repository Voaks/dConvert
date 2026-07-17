# Discord File Conversion Bot

A small Discord slash-command bot that converts uploaded files with the CloudConvert API.

## Setup

1. Install Python 3.11 or newer.
2. Create a virtual environment:

   ```powershell
   py -3 -m venv .venv
   ```

3. Install dependencies:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in:

   ```env
   DISCORD_TOKEN=your_discord_bot_token
   CLOUDCONVERT_API_KEY=your_cloudconvert_api_key
   ```

   When creating the CloudConvert API key, the token name can be anything. For scopes, check only:

   ```text
   task.read
   task.write
   ```

5. In the Discord Developer Portal, enable the bot and invite it with these scopes:

   ```text
   bot applications.commands
   ```

6. Start the bot:

   ```powershell
   .\.venv\Scripts\python.exe bot.py
   ```

   If `python` opens the Microsoft Store or fails on Windows, install Python from <https://www.python.org/downloads/> and check **Add python.exe to PATH** during installation.

## Usage

Use the slash command:

```text
/convert file:<attachment> output_format:pdf
```

Discord will show common target formats as choices for `output_format`.

Optional `input_format` can be used when CloudConvert cannot infer the source format from the filename.

CloudConvert export URLs expire after 24 hours. The bot uploads the converted result back to Discord when it is below `MAX_OUTPUT_UPLOAD_MB`; otherwise it returns the temporary download link.

## Notes

- Your CloudConvert API key needs only the `task.write` and `task.read` scopes.
- Set `DISCORD_GUILD_ID` in `.env` while developing if you want slash commands to appear instantly in one server. Without it, global command sync can take a while.
- Discord attachments are passed to CloudConvert by URL, so the attachment must remain reachable while the conversion job runs.
