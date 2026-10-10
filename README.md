# Ambient Email Agent

**It drafts the reply. You press the button.**

An AI assistant for your Gmail. It reads each new email, decides if it needs an answer, and writes a draft for you: a reply, or a meeting on your calendar. Then it sends the draft to you in Telegram. Nothing goes out until you tap **Accept**.

It also learns. Tell it "keep my replies short" once, and it remembers.

![How it works](docs/email-agent-map.png)

## What it does

- **Sorts your inbox.** Each email is "needs a reply", "good to know" or "ignore".
- **Writes drafts.** Replies in your style, plus meeting invites, moves and cancellations that fit your free time.
- **Asks you first.** Each draft comes to Telegram as a card with **Accept**, **Respond** and **Ignore** buttons.
- **Learns from you.** Your corrections update its saved preferences, so the next drafts are closer to what you want.
- **Runs on its own.** It checks Gmail every minute and clears out finished emails once a day.

## What you need

- A Mac or Linux computer
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and git. uv installs Python and all packages for you.
- An [OpenAI API key](https://developers.openai.com/api/docs/quickstart) with some credit. The agent uses `gpt-4.1`.
- A Telegram account
- A Google account whose Gmail the agent will read. This guide calls it **the mailbox**.
- A second email address, to send test emails from
- Optional: a [LangSmith](https://docs.langchain.com/langsmith/create-account-api-key) account, to see a log of each run

Setup takes about 30 minutes. Most of it is the Google part (step 4).

## Setup

Run each command from the project folder.

### Step 1. Download and install

```bash
git clone https://github.com/nikolay-bb/langgraph-email-agent.git
cd langgraph-email-agent
uv sync --group bot
```

✅ **Done when** there is no error and a `.venv` folder is there.
If you see `uv: command not found`, open a new terminal window and try again.

### Step 2. Create your Telegram bot

1. In Telegram, open [@BotFather](https://t.me/botfather) and send `/newbot`. Pick a name, and a username that ends in `bot`.
2. BotFather sends you a **token**: a long secret code. Copy it.
3. Open the chat with your new bot and tap **Start**.
4. Get your **chat ID**. Open this link in a private browser window, with your token in place of `<token>`:

   ```text
   https://api.telegram.org/bot<token>/getUpdates
   ```

   Find `"chat":{"id":` and copy the number after it. That is your chat ID.

✅ **Done when** the page starts with `{"ok":true` and shows your message.
If you see `"result":[]`, send your bot any message and reload the page.

The bot only answers your chat. It ignores messages from everyone else.

### Step 3. Add your keys

```bash
cp .env.example .env
chmod 600 .env
```

Open `.env` in a text editor and fill in these three lines:

| Setting | What to put |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `TELEGRAM_BOT_TOKEN` | The token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID from step 2 |

That is all you need. The other settings are optional:

<details>
<summary>Optional settings</summary>

| Setting | Default | What it does |
|---|---|---|
| `LANGSMITH_TRACING` | `false` | `true` sends a log of each run to LangSmith, email text included. Needs `LANGSMITH_API_KEY`. Keep this line in `.env`: with a key and no line, tracing turns on. |
| `LANGSMITH_API_KEY` | empty | Your LangSmith key. |
| `LANGSMITH_PROJECT` | `langgraph-email-agent` | The LangSmith project for the logs. |
| `LANGSMITH_ENDPOINT` | US region | For an EU account, use `https://eu.api.smith.langchain.com`. A key works only in its own region. |
| `TELEGRAM_ENABLE_EDIT` | `0` | `1` adds an **Edit** button to the cards. |
| `TELEGRAM_POLL_SECONDS` | `5` | How often the bot checks for new drafts. |
| `LANGGRAPH_URL` | `http://127.0.0.1:2024` | Where the bot finds the server. |
| `TELEGRAM_STATE_FILE` | `logs/telegram_bot_state.json` | Where the bot remembers its last list message. |
| `TELEGRAM_ALLOW_IPV6`, `GOOGLE_ALLOW_IPV6` | empty | The agent prefers IPv4. `1` uses your system's normal order. |

Do not change `TELEGRAM_ASSISTANT_ID`, `TELEGRAM_PREFS_ASSISTANT_ID` or `TELEGRAM_MEMORY_NAMESPACE`.

</details>

### Step 4. Connect Gmail and Calendar

Google must know about your app before it lets the app read your mail. You do this one time in the [Google Cloud console](https://console.cloud.google.com/).

1. **Create a project.** Menu > **IAM & Admin** > **Create a Project**.
2. **Turn on Gmail and Calendar.** Menu > **APIs & Services** > **Library**. Find **Gmail API** and click **Enable**. Do the same for **Google Calendar API**.
3. **Set up the sign-in screen.** Menu > **Google Auth platform** > **Branding** > **Get Started**. Enter an app name and your email. For **Audience**, pick **External**, then click **Create**. Next, open **Audience** > **Test users** > **Add users** and add the mailbox address.
4. **Create the app's login.** **Google Auth platform** > **Clients** > **Create Client**. Pick **Desktop app** and click **Create**. Download the JSON file.
5. **Put the file in place:**

   ```bash
   mkdir -p src/email_assistant/tools/gmail/.secrets
   mv <path-to-the-downloaded-file> src/email_assistant/tools/gmail/.secrets/secrets.json
   ```

6. **Sign in** (on a computer with a browser):

   ```bash
   uv run python src/email_assistant/tools/gmail/setup_gmail.py
   ```

   A browser opens. Sign in as the mailbox. Allow both permissions: Gmail (to read and send email) and Calendar (to see free time and manage meetings).

**See "Google hasn't verified this app"?** That is normal for your own app. Click **Advanced**, then **Go to (your app name) (unsafe)**.

✅ **Done when** the terminal says `Authentication successful!`
If you see `Error 403: access_denied`, the mailbox is not in **Test users**. If you see `Client secrets file not found`, check the file path in item 5.

> **Avoid the weekly sign-out.** While your Google app is in **Testing**, Google signs you out after 7 days. To stop this, open **Google Auth platform** > **Audience** and click **Publish app**. Then run `setup_gmail.py` one more time. For personal use, Google does not need to review your app ([Google's page](https://support.google.com/cloud/answer/13464323)). The warning screen stays.

### Step 5. Start it

Open two terminal windows.

**Window 1: the server**

```bash
uv run langgraph dev --no-browser
```

✅ **Done when** you see `API: http://127.0.0.1:2024`. Leave this window open.

**Window 2: the Telegram bot**

```bash
uv run --group bot python scripts/telegram_inbox.py
```

✅ **Done when** you see `answering only chat` and your chat ID. Send `/start` to your bot. It replies `Connected.`

### Step 6. Try it

1. From your second email address, send the mailbox a question. For example: "Can we meet on Thursday at 14:00 for 30 minutes?" Do not open it in Gmail: the agent only reads unread email.
2. In a third window, tell the agent to check the inbox now. Use the mailbox address:

   ```bash
   uv run python src/email_assistant/tools/gmail/run_ingest.py --email you@example.com --minutes-since 120
   ```

3. In Telegram, the bot shows `1 waiting`. Tap the email. You see the email and the draft. Tap a button.

✅ **Done when** the card shows `Proposed:` and a draft.

### Step 7. Make it automatic

With the server running, set two scheduled jobs:

```bash
uv run python src/email_assistant/tools/gmail/setup_cron.py --email you@example.com --schedule "* * * * *"
uv run python src/email_assistant/cleanup.py --schedule "30 3 * * *"
```

- The first checks Gmail **every minute**. Keep it at every minute if you can: each check also keeps the server's saved data up to date.
- The second clears out finished emails **every day at 03:30 UTC**. It never removes an email that waits for you.

✅ **Done when** you see `Cron job created successfully` and `Clean-up cron job created`.

That's it. Your agent now works on its own. 🎉

The jobs stay after a restart. Run a command again to change its schedule. To turn them off:

```bash
uv run python src/email_assistant/tools/gmail/setup_cron.py --delete
uv run python src/email_assistant/cleanup.py --delete
```

## How to use it

Each email that needs you shows up in Telegram as a card.

| Button | What it does |
|---|---|
| **Accept** | Does exactly what the card shows: sends the reply, or creates, moves or cancels the meeting. |
| **Respond** | Type what you want changed. The agent writes a new draft. If you say "always ...", it saves that as a preference and shows `Preference noted`. |
| **Ignore** | Does nothing and closes this email. |
| **Edit** | Change the draft's fields yourself, then tap Send. Needs `TELEGRAM_ENABLE_EDIT=1`. |
| **Full text** / **Back to list** | Shows the whole email / goes back to the list. |

After a reply is sent, the bot shows `Reply sent` and the email is marked as read in Gmail.

**Bot commands**

- `/list`: shows the emails that wait for you
- `/prefs`: shows what the agent learned about you, and lets you change it in plain words
- `/start`: checks that the bot is connected

## Make it yours

- **Who you are.** Edit `default_background` in `src/email_assistant/prompts.py`. The example is "Alex, a project manager".
- **Starting preferences.** In the same file, edit `default_triage_instructions` (what to answer, what to skip), `default_response_preferences` (how to write) and `default_cal_preferences` (how to plan meetings). Do this before the first email. After the first email, the agent uses its saved copy, so use `/prefs` in Telegram instead.
- **Time zone and work hours.** The defaults are `Europe/London`, 09:00 to 17:00. Change `MEETING_TIMEZONE` in `src/email_assistant/utils.py` and `WORK_START` / `WORK_END` in `src/email_assistant/tools/gmail/gmail_tools.py`. The prompts also say "UK time", "Europe/London" and "09:00 and 17:00" in words: update `prompts.py`, `tools/gmail/prompt_templates.py` and `email_date` in `utils.py` too.
- **Start fresh.** Stop the server and delete the `.langgraph_api/` folder. This removes the waiting emails, the learned preferences and the scheduled jobs.

Restart the server after each change.

## Run it 24/7 on a server (optional)

Use a small Linux machine that is always on and uses systemd. Do steps 1 to 6 on your own computer first: the Google sign-in needs a browser.

1. **Install on the server.** Open a new shell after the first command.

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   git clone https://github.com/nikolay-bb/langgraph-email-agent.git ~/langgraph-email-agent
   cd ~/langgraph-email-agent && uv sync --group bot
   ```

2. **Copy your keys** from your computer over SSH. Never send them by email or chat.

   ```bash
   scp .env YOUR_USER@YOUR_SERVER:langgraph-email-agent/.env
   scp -r src/email_assistant/tools/gmail/.secrets YOUR_USER@YOUR_SERVER:langgraph-email-agent/src/email_assistant/tools/gmail/
   ```

   Then, on the server, in the project folder:

   ```bash
   chmod 600 .env
   chmod -R go-rwx src/email_assistant/tools/gmail/.secrets
   ```

3. **Install the two services.** They start the server and the bot at boot, and restart them if they stop.

   ```bash
   for unit in email-agent-server email-agent-bot; do
     sed "s/YOUR_USER/$USER/g" deploy/$unit.service | sudo tee /etc/systemd/system/$unit.service > /dev/null
   done
   sudo systemctl daemon-reload
   sudo systemctl enable --now email-agent-server email-agent-bot
   ```

4. **Set the scheduled jobs** on the server (step 7). Each server keeps its own jobs.
5. **Watch the logs:**

   ```bash
   journalctl -u email-agent-server -f
   journalctl -u email-agent-bot -f
   ```

6. **Update** after a code change:

   ```bash
   cd ~/langgraph-email-agent && git pull && uv sync --group bot
   sudo systemctl restart email-agent-server email-agent-bot
   ```

**Good to know**

- **Run one bot only.** Stop the bot on your computer before you start it on the server. Otherwise Telegram shows a `Conflict` error.
- **Run one server only.** Two servers would answer each email twice.
- **Google sign-out every 7 days?** Your Google app is still in Testing (see step 4). Run `setup_gmail.py` on your computer and copy the new `token.json` to the server. No restart needed. On Google Cloud, `deploy/renew_gmail.sh` does both for you:

  ```bash
  GCP_PROJECT=<project> GCP_ZONE=<zone> VM_NAME=<machine> bash deploy/renew_gmail.sh
  ```

## Keep it safe

Please read this before you use the agent with your real inbox.

- **The server has no password.** Keep it on `127.0.0.1` (the default). Never use `--host 0.0.0.0` or `--tunnel`. A web page open in a browser on the same machine can also reach it, so a machine with no browser is safest.
- **No open ports needed.** The bot and the server only make outgoing connections.
- **Never share or commit `token.json`, `secrets.json` or `.env`.** `token.json` lets anyone read and send your email. These files are already in `.gitignore`: keep them there.
- **Your emails are stored on the machine**, in `.langgraph_api/`, in `logs/` and in the server log. Protect the machine and its backups.
- **OpenAI sees your email text** to write drafts ([OpenAI's data policy](https://developers.openai.com/api/docs/guides/your-data)). **LangSmith** sees it too if you turn tracing on. The LangGraph tool also sends usage data ([how to turn it off](https://docs.langchain.com/langsmith/data-storage-and-privacy)).
- **Read each card before you tap Accept.** An email can contain hidden instructions for the AI. Check the text, the Cc line and the attendees. The agent cannot choose who a reply goes to (always the sender), but it can add Cc addresses. These happen without a card: the calendar check, marking an email as read, and a preference update after you tap Respond (the bot shows you that update).
- **Set a spend limit** in your OpenAI account ([OpenAI's guide](https://developers.openai.com/api/docs/guides/spend-limits)).
- **Bot token leaked?** Send `/revoke` to @BotFather, put the new token in `.env`, and restart the bot.

## Costs

- **OpenAI** charges per AI call. Each new email costs one call to sort it. A reply costs a few more. An empty inbox costs nothing. See [gpt-4.1 prices](https://developers.openai.com/api/docs/models/gpt-4.1).
- **Google** Gmail and Calendar APIs are free for normal use, with [usage limits](https://developers.google.com/workspace/gmail/api/reference/quota).
- **LangSmith** (optional) has a free plan with a monthly limit ([pricing](https://docs.langchain.com/langsmith/billing)). Gmail checks do not count toward it. Each processed email does.
- **Hosting** is up to you. Any always-on machine works. It needs no public address.

## Troubleshooting

| Problem | Fix |
|---|---|
| `invalid_grant` or `Token has been expired or revoked` | Google signed you out. Run `setup_gmail.py` again. To stop this, publish your Google app (step 4). |
| `Error 403: access_denied` at Google sign-in | Add the mailbox under **Audience** > **Test users**. |
| `Could not find valid token data` | Do step 4, item 6. |
| No card in Telegram | Check: the bot runs, you tapped **Start**, `/start` gets an answer, the email is unread, recent, and sent from another address. If it asks for nothing, the agent may ignore it on purpose. |
| `No emails found matching the criteria` | The email is read or too old. Add `--include-read` or a bigger `--minutes-since`. |
| `Already processed ... skipped` | Add `--rerun` to process it again. |
| `Conflict` in the bot log | Two bots use the same token. Stop one. Also close any browser tab with `getUpdates`. |
| `Chat not found` in the bot log | Open the chat with your bot and tap **Start**. |
| `could not reach LangGraph` in the bot log | Start the server (step 5). |
| `setup_cron.py` or `cleanup.py` fails to connect | Start the server first, then try again. |
| The bot says `The run failed.` | Check the server window for the full error. |
| LangSmith errors in the server window | Fix `LANGSMITH_API_KEY` and `LANGSMITH_ENDPOINT`, or set `LANGSMITH_TRACING=false`. |

## Limits

- One mailbox and one Telegram chat.
- It reads email sent **to** the mailbox. Email where you are only in Cc can be missed.
- The scheduled check looks back 60 minutes. If the server was off for longer, check by hand with a bigger `--minutes-since`.
- If a scheduled Gmail check fails, you only see it in the server log.
- The AI model, time zone and work hours are set in the code, not in `.env`.
- The AI can make mistakes. That is why every action waits for you.
- `langgraph dev` is a [development server](https://docs.langchain.com/langsmith/local-dev-testing). It is great for personal use. For a team or a larger setup, use a server with a database.
- The server packages `langgraph-api` and `langgraph-runtime-inmem` use the Elastic License 2.0. Read it before you host this for other people.

## Credits and licence

Built on LangChain's [agents-from-scratch](https://github.com/langchain-ai/agents-from-scratch), the code for the LangChain Academy course "Project: Ambient Agents with LangGraph". This project adds the Telegram bot, learned preferences, the daily clean-up and the server setup, and largely rewrites the Gmail tools.

MIT licence, same as the original. See [LICENSE](LICENSE).
