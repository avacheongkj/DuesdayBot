"""
DuesdayBot - Telegram renewal reminder bot.

Skeleton bot that listens for incoming messages, responds to "hello", and
walks users through adding renewal items with /add. Renewals persist in
Supabase so they survive restarts.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil import parser as date_parser
from dotenv import load_dotenv
from supabase import Client, create_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Config -----------------------------------------------------------------
# Load environment variables from .env (keeps secrets out of source code)
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set. Add it to your .env file."
    )

# --- Logging -----------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Supabase -----------------------------------------------------------------
# If the connection fails or credentials are missing, `supabase` stays None and
# every DB-backed handler degrades to a friendly error instead of crashing.
supabase: Client | None = None
try:
    if SUPABASE_URL and SUPABASE_KEY:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Connected to Supabase")
    else:
        logger.warning(
            "SUPABASE_URL / SUPABASE_KEY not set — renewals will not be persisted"
        )
except Exception:
    logger.exception("Failed to connect to Supabase — renewals will not be persisted")
    supabase = None

TO_DUE_TABLE = "to_due"

# --- Group Conversation State (v2) -------------------------------------------
# Module-level dict to track group /add conversation state.
# Key: (user_id, group_id), Value: {"step": str, "data": dict}
# This persists across message updates in groups (unlike context.user_data).
GROUP_ADD_STATE: dict[tuple[str, str], dict] = {}


def get_group_state_key(user_id: str, group_id: str) -> tuple[str, str]:
    """Generate a key for storing group conversation state."""
    return (user_id, group_id)


def get_group_state(user_id: str, group_id: str) -> dict | None:
    """Retrieve group conversation state for a user in a group."""
    return GROUP_ADD_STATE.get(get_group_state_key(user_id, group_id))


def set_group_state(user_id: str, group_id: str, state: dict) -> None:
    """Store group conversation state for a user in a group."""
    GROUP_ADD_STATE[get_group_state_key(user_id, group_id)] = state


def clear_group_state(user_id: str, group_id: str) -> None:
    """Clear group conversation state for a user in a group."""
    GROUP_ADD_STATE.pop(get_group_state_key(user_id, group_id), None)


def get_scope(update: Update) -> tuple[str, str]:
    """Resolve (scope, scope_id) for the current chat.
    In a group/supergroup, everything is shared: scope_id is the group's chat id.
    In a 1-to-1 chat, scope_id is the requesting user's id and items stay personal."""
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        return "group", str(chat.id)
    return "personal", str(update.effective_user.id)



async def save_renewal(row: dict) -> str | None:
    """Insert a renewal row into Supabase. Returns the id on success, None on failure."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot save renewal")
        return None
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(TO_DUE_TABLE).insert(row).execute()
        )
        # Extract the id from the inserted row
        if response.data and len(response.data) > 0:
            return response.data[0].get("id")
        return None
    except Exception:
        logger.exception("Failed to save renewal to Supabase")
        return None


async def fetch_renewals_for_scope(scope: str, scope_id: str) -> list[dict] | None:
    """Fetch all renewals for a personal user or a group. None means the fetch failed.
    Personal queries also require group_id IS NULL, so a user's own group-added
    items never leak into their private /list."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot fetch renewals")
        return None
    try:
        if scope == "group":
            query = supabase.table(TO_DUE_TABLE).select("*").eq("group_id", scope_id)
        else:
            query = (
                supabase.table(TO_DUE_TABLE)
                .select("*")
                .eq("user_id", scope_id)
                .is_("group_id", "null")
            )
        response = await asyncio.to_thread(lambda: query.order("due_date").execute())
        return response.data
    except Exception:
        logger.exception(f"Failed to fetch renewals for scope={scope} scope_id={scope_id}")
        return None


async def fetch_renewal(renewal_id) -> dict | None:
    """Fetch a single renewal row by id. None if not found or the query failed."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot fetch renewal")
        return None
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(TO_DUE_TABLE)
            .select("*")
            .eq("id", renewal_id)
            .limit(1)
            .execute()
        )
        rows = response.data
        return rows[0] if rows else None
    except Exception:
        logger.exception(f"Failed to fetch renewal id={renewal_id}")
        return None


async def fetch_scoped_renewal(scope: str, scope_id: str, renewal_id) -> dict | None:
    """Fetch a renewal by id, but only if it belongs to the given personal user or
    group. This is what keeps /edit and /delete from touching another user's or
    group's to-dues. In a group, any member may act on any item in that group."""
    row = await fetch_renewal(renewal_id)
    if row is None:
        return None

    if scope == "group":
        owned = str(row.get("group_id")) == scope_id
    else:
        owned = row.get("group_id") is None and str(row.get("user_id")) == scope_id

    if not owned:
        logger.warning(
            f"scope={scope} scope_id={scope_id} attempted to access "
            f"renewal id={renewal_id} it doesn't own"
        )
        return None
    return row


async def update_renewal(renewal_id, fields: dict) -> bool:
    """Update a renewal row by id. Returns True on success."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot update renewal")
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(TO_DUE_TABLE)
            .update(fields)
            .eq("id", renewal_id)
            .execute()
        )
        return True
    except Exception:
        logger.exception(f"Failed to update renewal id={renewal_id}")
        return False


async def delete_renewal(renewal_id) -> bool:
    """Delete a renewal row by id. Returns True on success."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot delete renewal")
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(TO_DUE_TABLE).delete().eq("id", renewal_id).execute()
        )
        return True
    except Exception:
        logger.exception(f"Failed to delete renewal id={renewal_id}")
        return False


# --- PDPA consent (one row per Telegram user, separate from to_due items) -----
USERS_TABLE = "users"


async def get_user_consent(user_id: str) -> bool:
    """Whether the user has granted consent. Fails closed (False) on any DB issue."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot check consent")
        return False
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(USERS_TABLE)
            .select("user_consent")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = response.data
        return bool(rows and rows[0].get("user_consent"))
    except Exception:
        logger.exception(f"Failed to check consent for user_id={user_id}")
        return False


async def get_consented_user_ids() -> set[str] | None:
    """Set of user_ids with active consent. None means the query failed."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot load consented users")
        return None
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(USERS_TABLE)
            .select("user_id")
            .eq("user_consent", True)
            .execute()
        )
        return {row["user_id"] for row in response.data}
    except Exception:
        logger.exception("Failed to load consented users")
        return None


async def grant_user_consent(user_id: str) -> bool:
    """Record consent=true and consent_date=now() for a user. Returns True on success."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot record consent")
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(USERS_TABLE)
            .upsert(
                {
                    "user_id": user_id,
                    "user_consent": True,
                    "consent_date": datetime.now(timezone.utc).isoformat(),
                }
            )
            .execute()
        )
        logger.info(f"AUDIT: consent granted by user_id={user_id}")
        return True
    except Exception:
        logger.exception(f"Failed to record consent for user_id={user_id}")
        return False


# --- Group PDPA consent (one row per group chat, mirrors the users table) -----
GROUPS_TABLE = "groups"


async def get_group_consent(group_id: str) -> bool:
    """Whether the group has granted consent. Fails closed (False) on any DB issue."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot check group consent")
        return False
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(GROUPS_TABLE)
            .select("group_consent")
            .eq("group_id", group_id)
            .limit(1)
            .execute()
        )
        rows = response.data
        return bool(rows and rows[0].get("group_consent"))
    except Exception:
        logger.exception(f"Failed to check consent for group_id={group_id}")
        return False


async def get_consented_group_ids() -> set[str] | None:
    """Set of group_ids with active consent. None means the query failed."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot load consented groups")
        return None
    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(GROUPS_TABLE)
            .select("group_id")
            .eq("group_consent", True)
            .execute()
        )
        return {row["group_id"] for row in response.data}
    except Exception:
        logger.exception("Failed to load consented groups")
        return None


async def grant_group_consent(group_id: str) -> bool:
    """Record consent=true and consent_date=now() for a group. Returns True on success."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot record group consent")
        return False
    try:
        await asyncio.to_thread(
            lambda: supabase.table(GROUPS_TABLE)
            .upsert(
                {
                    "group_id": group_id,
                    "group_consent": True,
                    "consent_date": datetime.now(timezone.utc).isoformat(),
                }
            )
            .execute()
        )
        logger.info(f"AUDIT: consent granted for group_id={group_id}")
        return True
    except Exception:
        logger.exception(f"Failed to record consent for group_id={group_id}")
        return False


async def delete_scope_data(scope: str, scope_id: str) -> bool:
    """Hard-delete every to_due row and the consent record for a personal user
    or a group, matching whichever scope /stop was invoked in."""
    if supabase is None:
        logger.error("Supabase is not connected; cannot delete data")
        return False
    try:
        if scope == "group":
            await asyncio.to_thread(
                lambda: supabase.table(TO_DUE_TABLE).delete().eq("group_id", scope_id).execute()
            )
            await asyncio.to_thread(
                lambda: supabase.table(GROUPS_TABLE).delete().eq("group_id", scope_id).execute()
            )
            logger.info(f"AUDIT: all data permanently deleted for group_id={scope_id}")
        else:
            await asyncio.to_thread(
                lambda: supabase.table(TO_DUE_TABLE)
                .delete()
                .eq("user_id", scope_id)
                .is_("group_id", "null")
                .execute()
            )
            await asyncio.to_thread(
                lambda: supabase.table(USERS_TABLE).delete().eq("user_id", scope_id).execute()
            )
            logger.info(f"AUDIT: all data permanently deleted for user_id={scope_id}")
        return True
    except Exception:
        logger.exception(f"Failed to delete all data for scope={scope} scope_id={scope_id}")
        return False


async def require_consent(update: Update) -> bool:
    """Gate for data-storing operations (/add, reminder buttons, /edit), aware of
    whether the chat is personal or a group.
    Callers using a callback query should call query.answer() themselves first —
    this only sends the guidance message, so a query is never answered twice."""
    scope, scope_id = get_scope(update)

    if scope == "group":
        if await get_group_consent(scope_id):
            return True
        message = (
            "This group hasn't agreed to DuesdayBot's data collection yet. "
            "Send /start in this group to review and agree before continuing."
        )
    else:
        if await get_user_consent(scope_id):
            return True
        message = (
            "You haven't agreed to DuesdayBot's data collection yet. "
            "Send /start to review and agree before continuing."
        )

    if update.callback_query:
        await update.callback_query.message.reply_text(message)
    else:
        await update.effective_message.reply_text(message)
    return False


# --- Daily reminder job --------------------------------------------------------
REMINDER_HOUR = 8  # local time
REMINDER_MINUTE = 0

CATEGORY_ICON = {
    "subscription": "📺",
    "insurance": "🚗",
    "passport": "🛂",
    "credit_card": "💳",
    "other": "🔔",
}


def format_due_label(due_date: date) -> str:
    return f"{due_date.strftime('%B')} {due_date.day}, {due_date.year}"


def build_reminder_keyboard(renewal_id) -> InlineKeyboardMarkup:
    # One button per row so full labels always show, instead of Telegram
    # truncating them to fit three across a single row.
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✓ Done", callback_data=f"renewal_done:{renewal_id}")],
            [InlineKeyboardButton("⏸️ Snooze", callback_data=f"renewal_snooze:{renewal_id}")],
            [InlineKeyboardButton("🔍 Compare", callback_data=f"renewal_compare:{renewal_id}")],
        ]
    )


async def send_reminder(bot, row: dict, days_remaining: int) -> None:
    """Format and send a single renewal reminder to its owning user."""
    icon = CATEGORY_ICON.get(row.get("category"), "🔔")
    due_date = date.fromisoformat(row["due_date"])

    lines = [
        f"{icon} {row['name']}",
        f"Due: {format_due_label(due_date)} ({days_remaining} days away)",
        f"Owner: {row['owner']}",
    ]
    if row.get("link"):
        lines.append(f"Link: {row['link']}")
    if row.get("notes"):
        lines.append(f"📝 Notes: {row['notes']}")

    try:
        await bot.send_message(
            chat_id=int(row["user_id"]),
            text="\n".join(lines),
            reply_markup=build_reminder_keyboard(row.get("id")),
        )
        logger.info(
            f"Sent reminder for '{row['name']}' (id={row.get('id')}) "
            f"to user {row['user_id']}"
        )
    except Exception:
        logger.exception(
            f"Failed to send reminder for '{row['name']}' (id={row.get('id')}) "
            f"to user {row['user_id']}"
        )


async def check_upcoming_renewals(bot) -> None:
    """Daily job: find renewals due within their lead time and message each owner."""
    if supabase is None:
        logger.error("Supabase is not connected; skipping daily renewal check")
        return

    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(TO_DUE_TABLE).select("*").execute()
        )
        rows = response.data
    except Exception:
        logger.exception("Failed to query renewals for the daily reminder job")
        return

    # Fail closed: if we can't verify who has consented, don't send anything.
    consented_user_ids = await get_consented_user_ids()
    if consented_user_ids is None:
        logger.error("Skipping daily renewal check — could not verify consent")
        return

    today = date.today()
    reminders_sent = 0

    for row in rows:
        # Group-shared items get their own daily digest (check_group_reminders)
        # instead of a per-item DM with buttons — personal path stays untouched.
        if row.get("group_id"):
            continue

        if str(row.get("user_id")) not in consented_user_ids:
            continue

        try:
            due_date = date.fromisoformat(row["due_date"])
        except (TypeError, ValueError, KeyError):
            logger.warning(f"Skipping renewal with unparseable due_date: {row}")
            continue

        lead_time_days = row.get("lead_time_days") or 0
        days_remaining = (due_date - today).days

        if not (0 <= days_remaining <= lead_time_days):
            continue

        # `last_reminded` doubles as "snoozed until" — the Snooze button sets it
        # to a future date, so skip sending until that date has passed.
        last_reminded = row.get("last_reminded")
        if last_reminded:
            try:
                if date.fromisoformat(last_reminded) >= today:
                    continue
            except (TypeError, ValueError):
                pass

        await send_reminder(bot, row, days_remaining)
        reminders_sent += 1

    logger.info(f"Daily reminder job complete — {reminders_sent} reminder(s) sent")


async def check_group_reminders(bot) -> None:
    """Daily job: post one aggregated digest per group listing its upcoming to-dues.
    Unlike personal reminders, this has no Done/Snooze/Compare buttons and isn't
    suppressed by `last_reminded` — it's a plain recurring summary, not a per-item nag."""
    if supabase is None:
        logger.error("Supabase is not connected; skipping group reminder digest")
        return

    try:
        response = await asyncio.to_thread(
            lambda: supabase.table(TO_DUE_TABLE).select("*").execute()
        )
        rows = response.data
    except Exception:
        logger.exception("Failed to query renewals for the group reminder digest")
        return

    # Fail closed: if we can't verify which groups have consented, post nothing.
    consented_group_ids = await get_consented_group_ids()
    if consented_group_ids is None:
        logger.error("Skipping group reminder digest — could not verify group consent")
        return

    today = date.today()
    by_group: dict[str, list[tuple[dict, int]]] = {}

    for row in rows:
        group_id = row.get("group_id")
        if not group_id or str(group_id) not in consented_group_ids:
            continue

        try:
            due_date = date.fromisoformat(row["due_date"])
        except (TypeError, ValueError, KeyError):
            continue

        lead_time_days = row.get("lead_time_days") or 0
        days_remaining = (due_date - today).days
        if not (0 <= days_remaining <= lead_time_days):
            continue

        by_group.setdefault(str(group_id), []).append((row, days_remaining))

    groups_notified = 0
    for group_id, items in by_group.items():
        items.sort(key=lambda pair: pair[1])
        lines = []
        for row, days_remaining in items:
            line = (f"• {row['name']} - {format_due_label(date.fromisoformat(row['due_date']))} "
                    f"({days_remaining} days away)")
            if row.get("notes"):
                line += f"\n  📝 {row['notes']}"
            lines.append(line)
        text = "📋 Upcoming group to-dues:\n" + "\n".join(lines)

        try:
            await bot.send_message(chat_id=int(group_id), text=text)
            groups_notified += 1
        except Exception:
            logger.exception(f"Failed to send group digest to group_id={group_id}")

    logger.info(f"Group reminder digest complete — {groups_notified} group(s) notified")


# Renewal action conversation states (Done / Snooze follow-ups)
AWAITING_DONE_STATUS, AWAITING_NEW_DATE, AWAITING_SNOOZE_CHOICE, AWAITING_SNOOZE_DAYS = range(6, 10)

SNOOZE_PRESET_DAYS = {"1w": 7, "2w": 14}


def build_done_status_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Renewing", callback_data=f"renewal_done_renew:{renewal_id}"),
                InlineKeyboardButton("Cancelling", callback_data=f"renewal_done_cancel:{renewal_id}"),
            ]
        ]
    )


async def handle_done_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not await require_consent(update):
        return ConversationHandler.END
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Done button pressed for renewal id={renewal_id}")
    context.user_data["pending_renewal_id"] = renewal_id
    await query.message.reply_text(
        "What's the status? Are you renewing this or cancelling it?",
        reply_markup=build_done_status_keyboard(renewal_id),
    )
    return AWAITING_DONE_STATUS


async def handle_done_renewing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Done: Renewing selected for renewal id={renewal_id}")
    context.user_data["pending_renewal_id"] = renewal_id
    await query.message.reply_text("Great! When is the new to-due date? (e.g., 15 March 2028)")
    return AWAITING_NEW_DATE


async def handle_done_cancelling(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Done: Cancelling selected for renewal id={renewal_id}")
    context.user_data.pop("pending_renewal_id", None)

    row = await fetch_renewal(renewal_id)
    name = row["name"] if row else "That item"

    await query.message.reply_text(f"Understood! Removing {name} from your list.")

    deleted = await delete_renewal(renewal_id)
    if deleted:
        await query.message.reply_text(f"{name} cancelled and removed ✖️")
    else:
        await query.message.reply_text(
            "I couldn't reach the database, so that wasn't removed. Please try again."
        )
    return ConversationHandler.END


async def handle_done_new_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    new_date = parse_due_date(text)
    if new_date is None:
        await update.message.reply_text(
            "Sorry, I couldn't understand that date. Try something like "
            "'15 March 2028' or '2028-03-15'."
        )
        return AWAITING_NEW_DATE

    if date.fromisoformat(new_date) < date.today():
        await update.message.reply_text("That's in the past! When is it actually due?")
        return AWAITING_NEW_DATE

    renewal_id = context.user_data.pop("pending_renewal_id", None)
    logger.info(f"Setting new due_date={new_date} for renewal id={renewal_id}")
    updated = await update_renewal(
        renewal_id,
        {
            "due_date": new_date,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # Renewing clears any earlier snooze against the old due date.
            "last_reminded": None,
        },
    )

    if updated:
        await update.message.reply_text(
            f"Updated! New date set to {format_due_label(date.fromisoformat(new_date))} ✅"
        )
    else:
        await update.message.reply_text(
            "I couldn't reach the database, so that update didn't save. Please try again."
        )
    return ConversationHandler.END


def build_snooze_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 Week", callback_data=f"renewal_snooze_1w:{renewal_id}"),
                InlineKeyboardButton("2 Weeks", callback_data=f"renewal_snooze_2w:{renewal_id}"),
                InlineKeyboardButton("Custom", callback_data=f"renewal_snooze_custom:{renewal_id}"),
            ]
        ]
    )


async def handle_snooze_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not await require_consent(update):
        return ConversationHandler.END
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Snooze button pressed for renewal id={renewal_id}")
    context.user_data["pending_renewal_id"] = renewal_id
    await query.message.reply_text(
        "Snooze for how long?", reply_markup=build_snooze_keyboard(renewal_id)
    )
    return AWAITING_SNOOZE_CHOICE


async def apply_snooze(renewal_id, days: int) -> tuple[str, date | None]:
    """Compute and apply a snooze. Returns (status, snooze_until):
    status is 'ok', 'past_due' (snooze would land on/after the due date), or 'error'."""
    row = await fetch_renewal(renewal_id)
    if row is None:
        return "error", None

    try:
        due_date = date.fromisoformat(row["due_date"])
    except (TypeError, ValueError, KeyError):
        return "error", None

    snooze_until = date.today() + timedelta(days=days)
    if snooze_until >= due_date:
        return "past_due", snooze_until

    updated = await update_renewal(renewal_id, {"last_reminded": snooze_until.isoformat()})
    return ("ok" if updated else "error"), snooze_until


async def respond_to_snooze_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    renewal_id,
    status: str,
    snooze_until: date | None,
    *,
    via_message: bool,
) -> int:
    reply = update.message.reply_text if via_message else update.callback_query.message.reply_text

    if status == "ok":
        await reply(f"Snoozed until {format_due_label(snooze_until)} ⏸️")
        return ConversationHandler.END

    if status == "past_due":
        context.user_data["pending_renewal_id"] = renewal_id
        await reply(
            "Uh oh! That's past the due date! When should we remind you?",
            reply_markup=build_snooze_keyboard(renewal_id),
        )
        return AWAITING_SNOOZE_CHOICE

    await reply("I couldn't reach the database, so the snooze wasn't saved. Please try again.")
    return ConversationHandler.END


async def handle_snooze_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, renewal_id = query.data.split(":", 1)
    preset_key = action.split("_")[-1]  # "1w" or "2w"
    logger.info(f"Snooze preset '{preset_key}' pressed for renewal id={renewal_id}")

    status, snooze_until = await apply_snooze(renewal_id, SNOOZE_PRESET_DAYS[preset_key])
    return await respond_to_snooze_result(
        update, context, renewal_id, status, snooze_until, via_message=False
    )


async def handle_snooze_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Snooze 'Custom' pressed for renewal id={renewal_id}")
    context.user_data["pending_renewal_id"] = renewal_id
    await query.message.reply_text("How many days?")
    return AWAITING_SNOOZE_DAYS


async def handle_snooze_custom_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("Please reply with a whole number of days, like 3 or 10.")
        return AWAITING_SNOOZE_DAYS

    renewal_id = context.user_data.pop("pending_renewal_id", None)
    logger.info(f"Custom snooze of {text} day(s) requested for renewal id={renewal_id}")

    status, snooze_until = await apply_snooze(renewal_id, int(text))
    return await respond_to_snooze_result(
        update, context, renewal_id, status, snooze_until, via_message=True
    )


async def cancel_renewal_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Let the user bail out of a Done/Snooze follow-up with /cancel."""
    context.user_data.pop("pending_renewal_id", None)
    await update.message.reply_text("No worries, cancelled.")
    return ConversationHandler.END


def build_compare_followup_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("View Links", callback_data=f"compare_view_links:{renewal_id}"),
                InlineKeyboardButton("Back", callback_data=f"compare_back:{renewal_id}"),
            ]
        ]
    )


async def handle_compare_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await require_consent(update):
        return
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Compare button pressed for renewal id={renewal_id}")

    # Best-effort: flagging is optional, so a missing column shouldn't block the reply.
    flagged = await update_renewal(renewal_id, {"flagged_for_comparison": True})
    if not flagged:
        logger.warning(
            f"Could not set comparison flag for renewal id={renewal_id} "
            "(check that the 'flagged_for_comparison' column exists)"
        )

    await query.message.reply_text(
        "Flag saved for comparison 🏷️ You can manually compare providers.",
        reply_markup=build_compare_followup_keyboard(renewal_id),
    )


async def handle_compare_view_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Compare 'View Links' pressed for renewal id={renewal_id}")

    row = await fetch_renewal(renewal_id)
    if row is None:
        await query.message.reply_text(
            "I couldn't reach the database to look that up. Please try again."
        )
        return

    link = row.get("link")
    if link:
        await query.message.reply_text(f"Saved link for {row['name']}: {link}")
    else:
        await query.message.reply_text(f"No link saved for {row['name']}.")


async def handle_compare_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    renewal_id = query.data.split(":", 1)[1]
    logger.info(f"Compare 'Back' pressed for renewal id={renewal_id}")
    await query.message.reply_text("Okay, let me know if there's anything else you need.")


# --- shared: per-item list rendering (used by /list, /edit, /delete pickers) ---
def format_list_item(r: dict) -> str:
    link_text = r.get("link") or "—"
    notes_text = r.get("notes") or ""
    output = (
        f"{r['name']} ({r['category']})\n"
        f"Due: {r['due_date']}\n"
        f"Owner: {r['owner']}\n"
        f"Link: {link_text}\n"
        f"Reminders: {r['reminder_type']}, {r['lead_time_days']} day(s) before"
    )
    if notes_text:
        output += f"\nNotes: {notes_text}"
    return output


def format_renewal_summary(row: dict) -> str:
    due_date = date.fromisoformat(row["due_date"])
    return (
        f"{row['name']} - Due: {format_due_label(due_date)}, "
        f"Lead time: {row['lead_time_days']} days, Reminder: {row['reminder_type']}"
    )


async def send_owned_renewal_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE, build_keyboard
) -> None:
    """Send one message per to-due in scope (personal or the current group), each
    with its own action button."""
    scope, scope_id = get_scope(update)
    rows = await fetch_renewals_for_scope(scope, scope_id)

    if rows is None:
        await update.effective_message.reply_text(
            "I can't reach the database right now. Please try again shortly."
        )
        return

    if not rows:
        await update.effective_message.reply_text("No to-dues saved yet. Use /add to create one.")
        return

    for r in rows:
        await update.effective_message.reply_text(
            format_list_item(r), reply_markup=build_keyboard(r["id"])
        )


# --- /edit -----------------------------------------------------------------
(
    EDIT_AWAITING_FIELD_CHOICE,
    EDIT_AWAITING_DATE,
    EDIT_AWAITING_LEAD_TIME,
    EDIT_AWAITING_REMINDER_TYPE,
    EDIT_AWAITING_NOTES,
    EDIT_AWAITING_LINK,
    EDIT_AWAITING_CONTINUE,
) = range(10, 17)

REMINDER_TYPE_OPTIONS = ["single", "escalating", "weekly"]

EDIT_FIELD_PROMPT = "What would you like to change?"


def build_edit_pick_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Edit This One", callback_data=f"edit_pick:{renewal_id}")]]
    )


def build_edit_field_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📅 Date", callback_data="edit_field:date"),
                InlineKeyboardButton("⏱️ Lead Time", callback_data="edit_field:lead_time"),
            ],
            [
                InlineKeyboardButton("🔔 Reminder Type", callback_data="edit_field:reminder_type"),
                InlineKeyboardButton("📝 Notes", callback_data="edit_field:notes"),
            ],
            [InlineKeyboardButton("🔗 Link", callback_data="edit_field:link")],
            [InlineKeyboardButton("❌ Cancel", callback_data="edit_field:cancel")],
        ]
    )


def build_reminder_type_keyboard() -> InlineKeyboardMarkup:
    # One button per row so full labels always show clearly.
    labels = {
        "single": "Once before due date",
        "escalating": "Multiple reminders before due date",
        "weekly": "Weekly until due date",
    }
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(labels[option], callback_data=f"edit_reminder_type:{option}")]
            for option in REMINDER_TYPE_OPTIONS
        ]
    )


def build_after_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Done", callback_data="edit_flow_done"),
                InlineKeyboardButton("Edit More", callback_data="edit_flow_more"),
            ]
        ]
    )


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/edit: list the user's to-dues, each with an "Edit This One" button."""
    await send_owned_renewal_picker(update, context, build_edit_pick_keyboard)


async def handle_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: "Edit This One" (from /edit) or "✎️ Edit" (from /list).
    In a group, any member may edit any item in that group's shared list."""
    query = update.callback_query
    await query.answer()
    if not await require_consent(update):
        return ConversationHandler.END
    renewal_id = query.data.split(":", 1)[1]
    scope, scope_id = get_scope(update)
    logger.info(f"Edit picked for renewal id={renewal_id} by scope={scope} scope_id={scope_id}")

    row = await fetch_scoped_renewal(scope, scope_id, renewal_id)
    if row is None:
        await query.message.reply_text("Couldn't find that to-due. Check /list for current items.")
        return ConversationHandler.END

    context.user_data["edit_renewal_id"] = row["id"]
    await query.message.reply_text(format_renewal_summary(row))
    await query.message.reply_text(EDIT_FIELD_PROMPT, reply_markup=build_edit_field_keyboard())
    return EDIT_AWAITING_FIELD_CHOICE


async def edit_field_choice_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]
    logger.info(f"Edit field choice: {choice}")

    if choice == "cancel":
        context.user_data.pop("edit_renewal_id", None)
        await query.message.reply_text("Okay, no changes made.")
        return ConversationHandler.END

    if choice == "date":
        await query.message.reply_text("New due date? (e.g., 15 March 2028)")
        return EDIT_AWAITING_DATE

    if choice == "lead_time":
        await query.message.reply_text("How many days?")
        return EDIT_AWAITING_LEAD_TIME

    if choice == "reminder_type":
        await query.message.reply_text(
            "Pick a reminder type:", reply_markup=build_reminder_type_keyboard()
        )
        return EDIT_AWAITING_REMINDER_TYPE

    if choice == "notes":
        await query.message.reply_text("New notes? (or reply 'skip' to remove them)")
        return EDIT_AWAITING_NOTES

    # choice == "link"
    await query.message.reply_text("New URL? (or reply 'skip' to remove it)")
    return EDIT_AWAITING_LINK


async def edit_receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    new_date = parse_due_date(text)
    if new_date is None:
        await update.message.reply_text(
            "Sorry, I couldn't understand that date. Try something like "
            "'15 March 2028' or '2028-03-15'."
        )
        return EDIT_AWAITING_DATE

    if date.fromisoformat(new_date) < date.today():
        await update.message.reply_text("That's in the past! When is it actually due?")
        return EDIT_AWAITING_DATE

    renewal_id = context.user_data.get("edit_renewal_id")
    logger.info(f"Editing renewal id={renewal_id}: due_date -> {new_date}")
    updated = await update_renewal(
        renewal_id,
        {"due_date": new_date, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if updated:
        await update.message.reply_text(
            f"Date updated to {format_due_label(date.fromisoformat(new_date))} ✅",
            reply_markup=build_after_edit_keyboard(),
        )
        return EDIT_AWAITING_CONTINUE

    context.user_data.pop("edit_renewal_id", None)
    await update.message.reply_text(
        "I couldn't reach the database, so that update didn't save. Please try again."
    )
    return ConversationHandler.END


async def edit_receive_lead_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text(
            "Please reply with a positive whole number of days, like 3 or 14."
        )
        return EDIT_AWAITING_LEAD_TIME

    renewal_id = context.user_data.get("edit_renewal_id")
    days = int(text)
    logger.info(f"Editing renewal id={renewal_id}: lead_time_days -> {days}")
    updated = await update_renewal(
        renewal_id,
        {"lead_time_days": days, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if updated:
        await update.message.reply_text(
            f"Lead time updated to {days} days ✅", reply_markup=build_after_edit_keyboard()
        )
        return EDIT_AWAITING_CONTINUE

    context.user_data.pop("edit_renewal_id", None)
    await update.message.reply_text(
        "I couldn't reach the database, so that update didn't save. Please try again."
    )
    return ConversationHandler.END


async def edit_receive_reminder_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    reminder_type = query.data.split(":", 1)[1]
    renewal_id = context.user_data.get("edit_renewal_id")
    logger.info(f"Editing renewal id={renewal_id}: reminder_type -> {reminder_type}")
    updated = await update_renewal(
        renewal_id,
        {"reminder_type": reminder_type, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if updated:
        await query.message.reply_text(
            f"Reminder type updated to {reminder_type} ✅", reply_markup=build_after_edit_keyboard()
        )
        return EDIT_AWAITING_CONTINUE

    context.user_data.pop("edit_renewal_id", None)
    await query.message.reply_text(
        "I couldn't reach the database, so that update didn't save. Please try again."
    )
    return ConversationHandler.END


async def edit_receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    link = None if text.lower() == "skip" else text
    renewal_id = context.user_data.get("edit_renewal_id")
    logger.info(f"Editing renewal id={renewal_id}: link -> {link}")
    updated = await update_renewal(
        renewal_id,
        {"link": link, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if updated:
        await update.message.reply_text(
            f"Link updated to {link} ✅" if link else "Link cleared ✅",
            reply_markup=build_after_edit_keyboard(),
        )
        return EDIT_AWAITING_CONTINUE

    context.user_data.pop("edit_renewal_id", None)
    await update.message.reply_text(
        "I couldn't reach the database, so that update didn't save. Please try again."
    )
    return ConversationHandler.END


async def edit_receive_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    notes = None if text.lower() == "skip" else text
    renewal_id = context.user_data.get("edit_renewal_id")
    logger.info(f"Editing renewal id={renewal_id}: notes -> {notes}")
    updated = await update_renewal(
        renewal_id,
        {"notes": notes, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    if updated:
        await update.message.reply_text(
            f"Notes updated to {notes} ✅" if notes else "Notes cleared ✅",
            reply_markup=build_after_edit_keyboard(),
        )
        return EDIT_AWAITING_CONTINUE

    context.user_data.pop("edit_renewal_id", None)
    await update.message.reply_text(
        "I couldn't reach the database, so that update didn't save. Please try again."
    )
    return ConversationHandler.END


async def handle_edit_flow_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    renewal_id = context.user_data.get("edit_renewal_id")
    logger.info(f"Edit flow: Done pressed for renewal id={renewal_id}")

    # Fetch and display the edited renewal
    if renewal_id:
        renewal = await fetch_renewal(renewal_id)
        if renewal:
            summary = format_list_item(renewal)
            await query.message.reply_text(summary, reply_markup=build_list_item_keyboard(renewal_id))
        else:
            await query.message.reply_text("All set!")
    else:
        await query.message.reply_text("All set!")

    context.user_data.pop("edit_renewal_id", None)
    return ConversationHandler.END


async def handle_edit_flow_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    renewal_id = context.user_data.get("edit_renewal_id")
    logger.info(f"Edit flow: Edit More pressed for renewal id={renewal_id}")

    row = await fetch_renewal(renewal_id)
    if row is None:
        context.user_data.pop("edit_renewal_id", None)
        await query.message.reply_text(
            "I couldn't reach the database to look that up. Please try again."
        )
        return ConversationHandler.END

    # Combine summary and prompt into one message for clarity
    combined_text = f"{format_renewal_summary(row)}\n\n{EDIT_FIELD_PROMPT}"
    await query.message.reply_text(combined_text, reply_markup=build_edit_field_keyboard())
    return EDIT_AWAITING_FIELD_CHOICE


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Let the user bail out of the /edit flow with /cancel."""
    context.user_data.pop("edit_renewal_id", None)
    await update.message.reply_text("No worries, cancelled.")
    return ConversationHandler.END


# --- /delete -----------------------------------------------------------------
DELETE_AWAITING_CONFIRM = 16


def build_delete_pick_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Delete This One", callback_data=f"delete_pick:{renewal_id}")]]
    )


def build_delete_confirm_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, Delete", callback_data=f"delete_confirm_yes:{renewal_id}"
                ),
                InlineKeyboardButton(
                    "❌ No, Cancel", callback_data=f"delete_confirm_no:{renewal_id}"
                ),
            ]
        ]
    )


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delete: list the user's to-dues, each with a "Delete This One" button."""
    await send_owned_renewal_picker(update, context, build_delete_pick_keyboard)


async def handle_delete_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: "Delete This One" (from /delete) or "🗑️ Delete" (from /list).
    In a group, any member may delete any item in that group's shared list."""
    query = update.callback_query
    await query.answer()
    renewal_id = query.data.split(":", 1)[1]
    scope, scope_id = get_scope(update)
    logger.info(f"Delete picked for renewal id={renewal_id} by scope={scope} scope_id={scope_id}")

    row = await fetch_scoped_renewal(scope, scope_id, renewal_id)
    if row is None:
        await query.message.reply_text("Couldn't find that to-due. Check /list for current items.")
        return ConversationHandler.END

    context.user_data["delete_renewal_id"] = row["id"]
    context.user_data["delete_renewal_name"] = row["name"]
    await query.message.reply_text(
        f"Delete '{row['name']}'?", reply_markup=build_delete_confirm_keyboard(row["id"])
    )
    return DELETE_AWAITING_CONFIRM


async def handle_delete_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    renewal_id = context.user_data.pop("delete_renewal_id", None)
    name = context.user_data.pop("delete_renewal_name", "That item")
    logger.info(f"Confirmed delete for renewal id={renewal_id}")

    deleted = await delete_renewal(renewal_id)
    if deleted:
        await query.message.reply_text(f"{name} deleted ✖️")
    else:
        await query.message.reply_text(
            "I couldn't reach the database, so that wasn't removed. Please try again."
        )
    return ConversationHandler.END


async def handle_delete_confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    logger.info("Delete cancelled by user")
    context.user_data.pop("delete_renewal_id", None)
    context.user_data.pop("delete_renewal_name", None)
    await query.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def delete_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Let the user bail out of the /delete flow with /cancel."""
    context.user_data.pop("delete_renewal_id", None)
    context.user_data.pop("delete_renewal_name", None)
    await update.message.reply_text("No worries, cancelled.")
    return ConversationHandler.END


# Matches "hello" as a whole word, case-insensitive, anywhere in the message
HELLO_PATTERN = re.compile(r"\bhello\b", re.IGNORECASE)

# /add conversation states
ITEM, DATE, OWNER, NOTES, REMINDER_LEAD, REMINDER_COUNT = range(6)

# Accepts inputs like "7", "7 days", "2 weeks", "1 week"
LEAD_TIME_PATTERN = re.compile(
    r"^\s*(\d+)\s*(day|days|d|week|weeks|w)?\s*$", re.IGNORECASE
)
LEAD_UNIT_TO_DAYS = {"day": 1, "days": 1, "d": 1, "week": 7, "weeks": 7, "w": 7}

# Best-effort keyword -> category mapping for the free-text item/type reply.
# Checked in order, so more specific phrases (e.g. "credit card") come first.
CATEGORY_KEYWORDS: dict[str, str] = {
    "insurance": "insurance",
    "passport": "passport",
    "credit card": "credit_card",
    "card": "credit_card",
    "subscription": "subscription",
    "netflix": "subscription",
    "spotify": "subscription",
    "membership": "subscription",
}


def parse_lead_time(text: str) -> tuple[int, str] | None:
    """Parse a lead-time reply into (days, human-readable label), or None if invalid."""
    match = LEAD_TIME_PATTERN.match(text)
    if not match:
        return None

    amount = int(match.group(1))
    unit = (match.group(2) or "days").lower()
    days = amount * LEAD_UNIT_TO_DAYS[unit]

    unit_label = "week" if unit in ("week", "weeks", "w") else "day"
    if amount != 1:
        unit_label += "s"
    return days, f"{amount} {unit_label}"


def parse_due_date(text: str) -> str | None:
    """Parse a free-text date reply into an ISO YYYY-MM-DD string, or None if invalid."""
    try:
        return date_parser.parse(text, fuzzy=False).date().isoformat()
    except (ValueError, OverflowError):
        return None


def categorize_item(text: str) -> str:
    """Guess a category (subscription/insurance/passport/credit_card/other)."""
    lowered = text.lower()
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in lowered:
            return category
    return "other"


# --- Handlers -----------------------------------------------------------------
async def handle_hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with a greeting when a message contains "hello"."""
    await update.message.reply_text(
        "Hi there! 👋 I'm Duesday, your renewal reminder bot."
    )


ADD_ITEM_PROMPT = (
    "What's the item and type? (e.g., Netflix subscription, Home Insurance, "
    "Passport renewal, Credit card expiration)"
)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Kick off the /add conversation by asking for the item and type.
    Routes to group-specific handler if in a group, otherwise uses ConversationHandler."""
    scope, _ = get_scope(update)

    # For groups, use custom state tracking instead of ConversationHandler
    if scope == "group":
        await add_start_group(update, context)
        return

    # For 1-to-1, clear any existing state to allow restarting incomplete flows
    context.user_data.pop("new_renewal", None)

    # For personal chats, use ConversationHandler flow
    if not await require_consent(update):
        return ConversationHandler.END
    context.user_data["new_renewal"] = {}
    await update.message.reply_text(ADD_ITEM_PROMPT)
    return ITEM


async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_renewal"]["item"] = update.message.text
    await update.message.reply_text("When is it due? (e.g., 15 March 2027)")
    return DATE


async def add_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    due_date = parse_due_date(text)
    if due_date is None:
        await update.message.reply_text(
            "Sorry, I couldn't understand that date. Try something like "
            "'15 March 2027' or '2027-03-15'."
        )
        return DATE

    context.user_data["new_renewal"]["date_display"] = text
    context.user_data["new_renewal"]["due_date"] = due_date
    await update.message.reply_text("Who owns it? You, your partner or someone else?")
    return OWNER


async def add_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_renewal"]["owner"] = update.message.text
    await update.message.reply_text(
        "Any additional info? (links, who's involved, where to renew, etc. — or reply 'skip' if not)"
    )
    return NOTES


async def add_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    notes = update.message.text.strip()
    context.user_data["new_renewal"]["notes"] = None if notes.lower() == "skip" else notes
    await update.message.reply_text(
        "How far in advance should I start reminding you? (e.g., 7 days, 2 weeks)"
    )
    return REMINDER_LEAD


async def add_reminder_lead(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parsed = parse_lead_time(update.message.text)
    if parsed is None:
        await update.message.reply_text(
            "Sorry, I didn't catch that. Please reply like '7 days' or '2 weeks'."
        )
        return REMINDER_LEAD

    lead_days, lead_label = parsed
    context.user_data["new_renewal"]["reminder_lead_days"] = lead_days
    context.user_data["new_renewal"]["reminder_lead_label"] = lead_label
    await update.message.reply_text("How many reminders would you like? (e.g., 1, 2, 3)")
    return REMINDER_COUNT


async def add_reminder_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text(
            "Please reply with a whole number of reminders, like 1, 2, or 3."
        )
        return REMINDER_COUNT

    renewal = context.user_data.pop("new_renewal")
    renewal["reminder_count"] = int(text)
    renewal["reminder_type"] = "single" if renewal["reminder_count"] == 1 else "escalating"
    renewal["category"] = categorize_item(renewal["item"])

    scope, scope_id = get_scope(update)
    row = {
        # For groups, use group_id as user_id to avoid NOT NULL constraint
        "user_id": scope_id,
        "group_id": scope_id if scope == "group" else None,
        "name": renewal["item"],
        "due_date": renewal["due_date"],
        "owner": renewal["owner"],
        "notes": renewal["notes"],
        "lead_time_days": renewal["reminder_lead_days"],
        "reminder_type": renewal["reminder_type"],
        "category": renewal["category"],
    }
    renewal_id = await save_renewal(row)

    if renewal_id:
        shared_note = "\nAdded to the group's shared list!" if scope == "group" else ""
        notes_str = f"\n📝 Notes: {renewal['notes']}" if renewal["notes"] else ""
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✎️ Edit", callback_data=f"edit_pick:{renewal_id}"),
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_pick:{renewal_id}"),
                ]
            ]
        )
        await update.message.reply_text(
            f"Got it! {renewal['item']} is due {renewal['date_display']}, "
            f"owned by {renewal['owner']}. I'll send {renewal['reminder_count']} "
            f"reminder(s), starting {renewal['reminder_lead_label']} before it's due. "
            f"Saved!{notes_str}{shared_note}",
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            "I couldn't reach the database, so that renewal wasn't saved. "
            "Please try /add again in a moment."
        )
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Let the user bail out of the /add flow with /cancel."""
    context.user_data.pop("new_renewal", None)
    await update.message.reply_text("No worries, cancelled. Nothing was saved.")
    return ConversationHandler.END


# --- Group-specific /add handlers (v2: module-level state tracking) ---
async def add_start_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start /add flow in a group with persistent state tracking."""
    if not await require_consent(update):
        return
    user_id = str(update.effective_user.id)
    group_id = str(update.effective_chat.id)
    # Clear any existing state for this user/group to allow restarting the flow
    clear_group_state(user_id, group_id)
    set_group_state(user_id, group_id, {"step": "item", "data": {}})
    await update.message.reply_text(ADD_ITEM_PROMPT)


async def group_add_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages in group /add flow using module-level state tracking (v2)."""
    scope, _ = get_scope(update)
    if scope != "group":
        return

    user_id = str(update.effective_user.id)
    group_id = str(update.effective_chat.id)
    state = get_group_state(user_id, group_id)
    logger.info(f"Group message handler: user={user_id}, group={group_id}, state={state}, msg={update.message.text[:50]}")

    if state is None:
        logger.info(f"No state for user {user_id} in group {group_id}")
        return

    step = state.get("step")

    if step == "item":
        state["data"]["item"] = update.message.text
        state["step"] = "date"
        set_group_state(user_id, group_id, state)
        await update.message.reply_text("When is it due? (e.g., 15 March 2027)")

    elif step == "date":
        text = update.message.text.strip()
        due_date = parse_due_date(text)
        if due_date is None:
            await update.message.reply_text(
                "Sorry, I couldn't understand that date. Try something like "
                "'15 March 2027' or '2027-03-15'."
            )
            return
        state["data"]["date_display"] = text
        state["data"]["due_date"] = due_date
        state["step"] = "owner"
        set_group_state(user_id, group_id, state)
        await update.message.reply_text("Who owns it? You, your partner or someone else?")

    elif step == "owner":
        state["data"]["owner"] = update.message.text
        state["step"] = "notes"
        set_group_state(user_id, group_id, state)
        await update.message.reply_text(
            "Any additional info? (links, who's involved, where to renew, etc. — or reply 'skip' if not)"
        )

    elif step == "notes":
        notes = update.message.text.strip()
        state["data"]["notes"] = None if notes.lower() == "skip" else notes
        state["step"] = "lead_time"
        set_group_state(user_id, group_id, state)
        await update.message.reply_text(
            "How far in advance should I start reminding you? (e.g., 7 days, 2 weeks)"
        )

    elif step == "lead_time":
        parsed = parse_lead_time(update.message.text)
        if parsed is None:
            await update.message.reply_text(
                "Sorry, I didn't catch that. Please reply like '7 days' or '2 weeks'."
            )
            return
        lead_days, lead_label = parsed
        state["data"]["reminder_lead_days"] = lead_days
        state["data"]["reminder_lead_label"] = lead_label
        state["step"] = "reminder_count"
        set_group_state(user_id, group_id, state)
        await update.message.reply_text("How many reminders would you like? (e.g., 1, 2, 3)")

    elif step == "reminder_count":
        text = update.message.text.strip()
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text(
                "Please reply with a whole number of reminders, like 1, 2, or 3."
            )
            return

        data = state["data"]
        data["reminder_count"] = int(text)
        data["reminder_type"] = "single" if data["reminder_count"] == 1 else "escalating"
        data["category"] = categorize_item(data["item"])

        scope, scope_id = get_scope(update)
        row = {
            # For groups, use group_id as user_id to avoid NOT NULL constraint
            "user_id": scope_id,
            "group_id": scope_id if scope == "group" else None,
            "name": data["item"],
            "due_date": data["due_date"],
            "owner": data["owner"],
            "notes": data["notes"],
            "lead_time_days": data["reminder_lead_days"],
            "reminder_type": data["reminder_type"],
            "category": data["category"],
        }
        renewal_id = await save_renewal(row)

        if renewal_id:
            notes_str = f"\n📝 Notes: {data['notes']}" if data["notes"] else ""
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✎️ Edit", callback_data=f"edit_pick:{renewal_id}"),
                        InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_pick:{renewal_id}"),
                    ]
                ]
            )
            await update.message.reply_text(
                f"Got it! {data['item']} is due {data['date_display']}, "
                f"owned by {data['owner']}. I'll send {data['reminder_count']} "
                f"reminder(s), starting {data['reminder_lead_label']} before it's due. "
                f"Saved!{notes_str}\nAdded to the group's shared list!",
                reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(
                "I couldn't reach the database, so that renewal wasn't saved. "
                "Please try /add again in a moment."
            )

        clear_group_state(user_id, group_id)


HELP_TEXT = (
    "Here's what I can do:\n\n"
    "/add - Add a new to-due item\n"
    "/list - View everything you've saved\n"
    "/edit - Change the date, lead time, reminder type, or link on an existing to-due\n"
    "/delete - Remove a to-due\n"
    "/export - Download all your data as a JSON file\n"
    "/privacy - View your PDPA data rights and the privacy policy\n"
    "/checknow - Manually run the daily reminder check\n"
    "/stop - Permanently delete all your data and stop using DuesdayBot\n"
    "/cancel - Cancel whatever you're in the middle of\n\n"
    "Every morning at 8am, I'll check what's coming up and remind you with buttons "
    "to mark it done, snooze it, or flag it for comparison.\n\n"
    "Add me to a group and /add, /list, /edit, and /delete will work on that group's "
    "own shared list instead of your personal one — every member can see and manage it, "
    "and the group gets a daily digest of what's coming up instead of individual reminders."
)

WELCOME_TEXT = "Welcome to Duesday! 👋 Your to-due reminder bot. What would you like to do?"

PRIVACY_POLICY_URL = "https://github.com/avacheongkj/DuesdayBot/blob/main/PRIVACY.md"

PDPA_NOTICE_TEXT = (
    "📋 DuesdayBot collects the following data:\n"
    "- Your Telegram user ID and chat history\n"
    "- To-due dates and renewal information\n"
    "- Reminder preferences\n"
    "- Link and note data you provide\n\n"
    "Why? To send you reminders and manage your to-dues.\n\n"
    "Your data is stored securely and never sold to third parties.\n\n"
    f"📖 Read full privacy policy: {PRIVACY_POLICY_URL}\n\n"
    "Do you agree to this data collection?"
)


def build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Add To-Due", callback_data="start_add"),
                InlineKeyboardButton("📋 View List", callback_data="start_list"),
                InlineKeyboardButton("❓ Help", callback_data="start_help"),
            ]
        ]
    )


def build_consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Agree & Continue", callback_data="consent_agree")],
            [InlineKeyboardButton("❌ Decline", callback_data="consent_decline")],
        ]
    )


GROUP_WELCOME_TEXT = (
    "Welcome to Duesday for Groups! 👋 To-dues added here are shared with everyone "
    "in this chat. What would you like to do?"
)

GROUP_PDPA_NOTICE_TEXT = (
    "📋 DuesdayBot collects the following data for this group:\n"
    "- This group's chat ID and message history related to to-dues\n"
    "- To-due dates and renewal information added by any member\n"
    "- Reminder preferences\n"
    "- Link and note data members provide\n\n"
    "Why? To send this group reminders and manage its shared to-dues. "
    "Anyone in this group can add, edit, or delete shared to-dues.\n\n"
    "This group's data is stored securely and never sold to third parties.\n\n"
    f"📖 Read full privacy policy: {PRIVACY_POLICY_URL}\n\n"
    "Does this group agree to this data collection? Any member can respond on the group's behalf."
)


def build_group_consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Agree & Continue", callback_data="group_consent_agree")],
            [InlineKeyboardButton("❌ Decline", callback_data="group_consent_decline")],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start: show the PDPA notice on first use, or the menu if already consented.
    Groups get their own one-time, group-level consent instead of a personal one."""
    scope, scope_id = get_scope(update)

    if scope == "group":
        if await get_group_consent(scope_id):
            await update.message.reply_text(GROUP_WELCOME_TEXT, reply_markup=build_start_keyboard())
            return
        await update.message.reply_text(
            GROUP_PDPA_NOTICE_TEXT, reply_markup=build_group_consent_keyboard()
        )
        return

    if await get_user_consent(scope_id):
        await update.message.reply_text(WELCOME_TEXT, reply_markup=build_start_keyboard())
        return

    await update.message.reply_text(PDPA_NOTICE_TEXT, reply_markup=build_consent_keyboard())


async def handle_consent_agree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)

    granted = await grant_user_consent(user_id)
    if not granted:
        await query.message.reply_text(
            "I couldn't reach the database to save your consent. Please try /start again in a moment."
        )
        return

    await query.message.reply_text(WELCOME_TEXT, reply_markup=build_start_keyboard())


async def handle_consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    logger.info(f"AUDIT: consent declined by user_id={update.effective_user.id}")
    await query.message.reply_text(
        "You've declined data collection. You cannot use DuesdayBot without consent. "
        "You can change your mind anytime by restarting."
    )


async def handle_group_consent_agree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = str(update.effective_chat.id)

    granted = await grant_group_consent(group_id)
    if not granted:
        await query.message.reply_text(
            "I couldn't reach the database to save this group's consent. "
            "Please try /start again in a moment."
        )
        return

    await query.message.reply_text(GROUP_WELCOME_TEXT, reply_markup=build_start_keyboard())


async def handle_group_consent_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    logger.info(f"AUDIT: consent declined for group_id={update.effective_chat.id}")
    await query.message.reply_text(
        "This group has declined data collection. DuesdayBot can't be used here without "
        "consent. Any member can change this anytime by sending /start again."
    )


STOP_WARNING_TEXT = (
    "⚠️ Warning: This will permanently delete ALL your data:\n"
    "• All to-dues\n"
    "• All reminders\n"
    "• Your consent record\n"
    "This action CANNOT be undone."
)

GROUP_STOP_WARNING_TEXT = (
    "⚠️ Warning: This will permanently delete ALL of this group's data:\n"
    "• All group to-dues\n"
    "• All group reminders\n"
    "• This group's consent record\n"
    "This action CANNOT be undone, and any member can trigger it."
)


def build_stop_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Yes, Delete Everything", callback_data="stop_confirm_yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="stop_confirm_no")],
        ]
    )


async def stop_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scope, _ = get_scope(update)
    text = GROUP_STOP_WARNING_TEXT if scope == "group" else STOP_WARNING_TEXT
    await update.message.reply_text(text, reply_markup=build_stop_confirm_keyboard())


async def handle_stop_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    scope, scope_id = get_scope(update)
    logger.info(f"AUDIT: full data deletion requested for scope={scope} scope_id={scope_id}")

    deleted = await delete_scope_data(scope, scope_id)
    if deleted:
        if scope == "group":
            await query.message.reply_text(
                "✅ All of this group's data has been permanently deleted. "
                "DuesdayBot is no longer tracking to-dues here."
            )
        else:
            await query.message.reply_text(
                "✅ All your data has been permanently deleted. You're no longer using DuesdayBot."
            )
    else:
        await query.message.reply_text(
            "I couldn't reach the database, so your data wasn't deleted. Please try /stop again."
        )


async def handle_stop_confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Cancelled. Your data is safe.")


# --- /export -----------------------------------------------------------------
EXPORT_FIELDS = [
    "name",
    "due_date",
    "category",
    "owner",
    "link",
    "lead_time_days",
    "reminder_type",
    "created_at",
    "updated_at",
]


def build_export_payload(rows: list[dict]) -> list[dict]:
    return [{field: r.get(field) for field in EXPORT_FIELDS} for r in rows]


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scope, scope_id = get_scope(update)
    logger.info(
        f"AUDIT: export requested for scope={scope} scope_id={scope_id} "
        f"at {datetime.now(timezone.utc).isoformat()}"
    )

    await update.message.reply_text("Preparing your data export...")

    rows = await fetch_renewals_for_scope(scope, scope_id)
    if rows is None:
        await update.message.reply_text(
            "I couldn't reach the database to prepare your export. Please try again shortly."
        )
        return

    payload = build_export_payload(rows)
    file_bytes = json.dumps(payload, indent=2).encode("utf-8")
    filename = f"DuesdayBot_Export_{date.today().isoformat()}.json"

    await update.message.reply_document(document=io.BytesIO(file_bytes), filename=filename)
    exported_what = "This group's to-dues" if scope == "group" else "Your to-dues"
    await update.message.reply_text(
        f"{exported_what} exported! This file contains all the data. "
        "You can use this to back up or import elsewhere."
    )


# --- /privacy -----------------------------------------------------------------
PRIVACY_TEXT = (
    "📖 *Your Privacy Rights Under PDPA:*\n\n"
    "✅ *Right to Access* - Request a copy of your data with /export\n"
    "✅ *Right to Correct* - Use /edit to update any information\n"
    "✅ *Right to Delete* - Use /stop to permanently delete all your data\n"
    "✅ *Right to Withdraw Consent* - Use /stop at any time\n\n"
    "*Full Privacy Policy:*\n"
    f"{PRIVACY_POLICY_URL}"
)


def build_privacy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Read Full Policy", url=PRIVACY_POLICY_URL)]]
    )


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Privacy policy viewed by user_id={update.effective_user.id}")
    await update.message.reply_text(
        PRIVACY_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_privacy_keyboard(),
    )


async def start_add_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: the /start menu's "Add To-Due" button starts the /add flow."""
    query = update.callback_query
    await query.answer()
    if not await require_consent(update):
        return ConversationHandler.END
    logger.info(f"Start menu: Add To-Due pressed by user {update.effective_user.id}")
    context.user_data["new_renewal"] = {}
    await query.message.reply_text(ADD_ITEM_PROMPT)
    return ITEM


async def start_list_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    logger.info(f"Start menu: View List pressed by user {update.effective_user.id}")
    await list_renewals(update, context)


async def start_help_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    logger.info(f"Start menu: Help pressed by user {update.effective_user.id}")
    await query.message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    logger.info(f"Help command by user {update.effective_user.id}")
    await update.message.reply_text(HELP_TEXT)


def build_list_item_keyboard(renewal_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✎️ Edit", callback_data=f"edit_pick:{renewal_id}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_pick:{renewal_id}"),
            ]
        ]
    )


def build_empty_list_keyboard() -> InlineKeyboardMarkup:
    # Reuses the same "start_add" entry point as the /start menu's Add button.
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ Add Your First To-Due", callback_data="start_add")]]
    )


async def list_renewals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show everything in scope (personal, or the current group's shared list),
    read live from Supabase. Each to-due is its own message so its Edit/Delete
    buttons can target it directly.

    Called both as the /list command (update.message is set) and from the /start
    menu's "View List" button (update.message is None; only update.callback_query
    is set for button presses). update.effective_message covers both cases."""
    scope, scope_id = get_scope(update)
    rows = await fetch_renewals_for_scope(scope, scope_id)

    if rows is None:
        await update.effective_message.reply_text(
            "Oops! Couldn't fetch your to-dues. Try again in a moment."
        )
        return

    if not rows:
        await update.effective_message.reply_text(
            "Hmm... Nothing here yet! 😌 Add your first to-due to get started.",
            reply_markup=build_empty_list_keyboard(),
        )
        return

    for r in rows:
        await update.effective_message.reply_text(
            format_list_item(r), reply_markup=build_list_item_keyboard(r["id"])
        )


async def check_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the daily reminder check (personal + group digest) — handy
    for testing without waiting until 8am. Safe to remove once the schedule is
    confirmed working."""
    await update.message.reply_text("Running the renewal check now...")
    await check_upcoming_renewals(context.bot)
    await check_group_reminders(context.bot)
    await update.message.reply_text("Done. Check the terminal log for details.")


scheduler = AsyncIOScheduler()


async def on_startup(application: Application) -> None:
    """Start the APScheduler jobs once the bot's event loop is running."""
    scheduler.add_job(
        check_upcoming_renewals,
        trigger=CronTrigger(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
        args=[application.bot],
        id="daily_renewal_check",
        replace_existing=True,
    )
    scheduler.add_job(
        check_group_reminders,
        trigger=CronTrigger(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
        args=[application.bot],
        id="daily_group_reminder_digest",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        f"Daily reminder jobs (personal + group digest) scheduled for "
        f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d} local time"
    )


# --- App setup -----------------------------------------------------------------
def build_application() -> Application:
    """Create and configure the Telegram Application with its handlers."""
    application = (
        Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(on_startup).build()
    )

    # /add walks the user through a multi-step Q&A to capture a renewal item.
    # The /start menu's "Add To-Due" button is a second entry point into the same flow.
    add_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CallbackQueryHandler(start_add_button, pattern=r"^start_add$"),
        ],
        states={
            ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item)],
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_date)],
            OWNER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_owner)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes)],
            REMINDER_LEAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder_lead)
            ],
            REMINDER_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_reminder_count)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", add_cancel),
        ],
    )

    # Done/Snooze reminder-button flows, each with a text or button follow-up
    renewal_action_conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_done_pressed, pattern=r"^renewal_done:"),
            CallbackQueryHandler(handle_snooze_pressed, pattern=r"^renewal_snooze:"),
        ],
        states={
            AWAITING_DONE_STATUS: [
                CallbackQueryHandler(handle_done_renewing, pattern=r"^renewal_done_renew:"),
                CallbackQueryHandler(handle_done_cancelling, pattern=r"^renewal_done_cancel:"),
            ],
            AWAITING_NEW_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_done_new_date)
            ],
            AWAITING_SNOOZE_CHOICE: [
                CallbackQueryHandler(handle_snooze_preset, pattern=r"^renewal_snooze_(1w|2w):"),
                CallbackQueryHandler(handle_snooze_custom_prompt, pattern=r"^renewal_snooze_custom:"),
            ],
            AWAITING_SNOOZE_DAYS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_snooze_custom_days)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_renewal_action),
        ],
    )

    # /edit: pick an existing to-due by name, then change one field
    # Entry point is the "Edit This One" / "✎️ Edit" button, not the /edit command
    # itself — /edit just lists items with that button (see edit_start).
    edit_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_edit_pick, pattern=r"^edit_pick:")],
        states={
            EDIT_AWAITING_FIELD_CHOICE: [
                CallbackQueryHandler(edit_field_choice_button, pattern=r"^edit_field:")
            ],
            EDIT_AWAITING_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_date)
            ],
            EDIT_AWAITING_LEAD_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_lead_time)
            ],
            EDIT_AWAITING_REMINDER_TYPE: [
                CallbackQueryHandler(edit_receive_reminder_type, pattern=r"^edit_reminder_type:")
            ],
            EDIT_AWAITING_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_notes)
            ],
            EDIT_AWAITING_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_link)
            ],
            EDIT_AWAITING_CONTINUE: [
                CallbackQueryHandler(handle_edit_flow_done, pattern=r"^edit_flow_done$"),
                CallbackQueryHandler(handle_edit_flow_more, pattern=r"^edit_flow_more$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", edit_cancel),
        ],
    )

    # Same pattern: entry point is the "Delete This One" / "🗑️ Delete" button.
    delete_conversation = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_delete_pick, pattern=r"^delete_pick:")],
        states={
            DELETE_AWAITING_CONFIRM: [
                CallbackQueryHandler(handle_delete_confirm_yes, pattern=r"^delete_confirm_yes:"),
                CallbackQueryHandler(handle_delete_confirm_no, pattern=r"^delete_confirm_no:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", delete_cancel),
        ],
    )

    # Group /add flow handler (v2: module-level state tracking)
    application.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.GROUP & ~filters.COMMAND, group_add_message_handler)
    )

    # Register all command handlers at app level FIRST, before ConversationHandlers.
    # This ensures any command sent at any time interrupts the current flow,
    # and the latest command always wins, regardless of sequence.
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("add", add_start))
    application.add_handler(CommandHandler("list", list_renewals))
    application.add_handler(CommandHandler("edit", edit_start))
    application.add_handler(CommandHandler("delete", delete_start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("privacy", privacy_command))
    application.add_handler(CommandHandler("checknow", check_now))
    application.add_handler(CommandHandler("stop", stop_start))

    # ConversationHandlers registered after app-level commands, so commands always take priority
    application.add_handler(add_conversation)
    application.add_handler(renewal_action_conversation)
    application.add_handler(edit_conversation)
    application.add_handler(delete_conversation)
    application.add_handler(CallbackQueryHandler(handle_compare_pressed, pattern=r"^renewal_compare:"))
    application.add_handler(
        CallbackQueryHandler(handle_compare_view_links, pattern=r"^compare_view_links:")
    )
    application.add_handler(CallbackQueryHandler(handle_compare_back, pattern=r"^compare_back:"))
    application.add_handler(CallbackQueryHandler(start_list_button, pattern=r"^start_list$"))
    application.add_handler(CallbackQueryHandler(start_help_button, pattern=r"^start_help$"))
    application.add_handler(CallbackQueryHandler(handle_consent_agree, pattern=r"^consent_agree$"))
    application.add_handler(
        CallbackQueryHandler(handle_consent_decline, pattern=r"^consent_decline$")
    )
    application.add_handler(
        CallbackQueryHandler(handle_group_consent_agree, pattern=r"^group_consent_agree$")
    )
    application.add_handler(
        CallbackQueryHandler(handle_group_consent_decline, pattern=r"^group_consent_decline$")
    )
    application.add_handler(
        CallbackQueryHandler(handle_stop_confirm_yes, pattern=r"^stop_confirm_yes$")
    )
    application.add_handler(
        CallbackQueryHandler(handle_stop_confirm_no, pattern=r"^stop_confirm_no$")
    )

    # Add new handlers here as the bot grows (commands, callbacks, etc.)
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(HELLO_PATTERN), handle_hello)
    )

    return application


def main() -> None:
    application = build_application()
    logger.info("DuesdayBot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
