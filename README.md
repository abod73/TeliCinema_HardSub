# TeliCinema_HardSub

Google Colab project to download a Telegram video and its direct-reply Arabic SRT, hardcode subtitles using FFmpeg, and send the generated MP4 back to the owner.

## Prerequisites

- Google Colab account
- Telegram account with API ID and API hash
- A Telegram bot token from @BotFather
- A private Telegram group containing your personal account and the bot
- A valid Arabic .srt subtitle file

## Setup Telegram credentials

1. Go to https://my.telegram.org
2. Log in with your personal Telegram account
3. Open API development tools
4. Copy API ID and API hash
5. Create a bot with @BotFather and copy the token
6. Add the bot and your personal account to a private group
7. Find the correct group ID using a Telegram ID utility or @RawDataBot

## How to run

1. Upload or open the notebook in Google Colab
2. Run Cell 1 to install dependencies
3. Run Cell 2 to enter credentials
4. Run Cell 3 to generate config.py
5. Run Cell 4 to generate subtitle_processor.py
6. Run Cell 5 to generate bot.py
7. Run Cell 6 to authenticate your personal Telegram account
8. Run Cell 7 to start the bot
9. Send a video to the private group
10. Reply with the Arabic .srt file directly to that video
11. Receive the hardcoded MP4 in your private chat

## Telegram workflow

- Add the bot and your personal user to the private group
- Send a video
- Reply with the .srt to the exact video message
- The app validates the pair and starts processing
- FFmpeg renders Arabic subtitles into the video
- The output is sent back to the owner’s private chat

## Troubleshooting

- If the subtitle is malformed, check UTF-8 or UTF-8-BOM encoding
- If Arabic appears incorrectly, check the font installed in the Colab environment
- If the session expires, re-run authentication and keep the session file private
- If Telegram rejects the upload, ensure the output file size is within the current limits
- If FFmpeg fails, verify the source file is valid and not corrupted

## Reconnect after Colab disconnect

- Google Colab is temporary; files can be deleted during disconnects or resets
- Re-open the notebook, enter credentials, and re-authenticate the session if needed
- For better continuity, store the session file in a secure Google Drive folder if desired
- Never publish or share the Telegram session file

## Security reminder

- Never upload your Telegram session file publicly
- Never share bot tokens, API hashes, or credentials
- Restrict the bot to a private group and owner-only commands

## Known limitations

- Telegram upload/download limits still apply; large files may fail
- FFmpeg rendering quality depends on the input video and subtitle timing
- Colab may disconnect and require re-authentication