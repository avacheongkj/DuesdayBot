# DuesdayBot

Your personal renewal and to-due reminder bot for Telegram.

![Status](https://img.shields.io/badge/status-active-brightgreen)

## What is DuesdayBot?

Life admin doesn't wait for a good time. DuesdayBot does the remembering, so you can do the deciding.

Track renewals, subscriptions, expirations, and important dates. Get daily reminders at 9am. Manage everything in Telegram.

**Features:**
- 📋 Track to-dues: subscriptions, insurance, passport renewals, credit card expirations, etc.
- 🔔 Daily reminders at 9am with customizable lead times
- ⏸️ Snooze reminders for 1-2 weeks or custom duration
- 📱 Works in 1-to-1 chats (personal) and groups (shared)
- 🔐 PDPA-compliant with explicit consent and right to delete
- 📊 Export your data anytime
- 🛡️ End-to-end secure with encrypted storage

## Quick Start

1. **Open Telegram** and search for `@DuesdayBot`
2. **Tap /start** and agree to the privacy policy
3. **Add your first to-due:** Type `/add`
4. **Get reminders:** DuesdayBot messages you at 9am daily

## Commands

- `/start` — Start the bot and review privacy rights
- `/add` — Add a new to-due (subscription, renewal, expiration, etc.)
- `/list` — View all your to-dues
- `/edit` — Update an existing to-due
- `/delete` — Remove a to-due
- `/export` — Download all your data as JSON (right to access)
- `/privacy` — View your PDPA rights
- `/stop` — Delete all your data permanently (right to be forgotten)

## Usage Examples

### Add an insurance renewal
/add
What's the item and type? → Car Insurance
When is it due? → 15 March 2027
Who owns it? → me
Any link to save? → https://insurance-provider.com/renew
How many days before reminder? → 30
How many reminders? → escalating

### Use in a group
Add @DuesdayBot to a group chat. All group members can create shared to-dues everyone sees reminders for (e.g., team project deadlines, shared bills).

## Privacy & Data

DuesdayBot is PDPA-compliant. You own your data:
- **No ads, no tracking, no selling data**
- **Explicit consent:** You agree when you /start
- **Right to access:** Export your data anytime with /export
- **Right to delete:** Permanently delete everything with /stop
- **Transparent:** Full privacy policy available at [PRIVACY.md](https://github.com/avacheongkj/DuesdayBot/blob/main/PRIVACY.md)

### Data Persistence

Clearing your Telegram chat history does **NOT** delete your to-dues or data with DuesdayBot.
Your data is stored securely in our database and persists even if you clear the chat. To permanently delete all your data, use the `/stop` command.

## Technical Details

Built with:
- **Python 3** with python-telegram-bot library
- **Supabase** (PostgreSQL database)
- **Railway** (hosting & deployment)

Data is encrypted in transit and at rest. RLS (Row Level Security) ensures users only see their own data.

## Support

Questions or issues?
- 📧 Email: avackj1999@gmail.com
- 💬 Telegram: @DuesdayBot

## License

All Rights Reserved. This code is provided as-is for the DuesdayBot service on Telegram.
Copying, forking, or redistributing the code without permission is prohibited.

---

**Made with ❤️ by Ava**
