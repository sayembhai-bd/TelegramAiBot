import os
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    ChatMemberHandler,
)

# =========================
# BOT SETTINGS
# =========================

# এখানে BotFather থেকে পাওয়া Bot Token বসাও
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

# Welcome message পরিবর্তন করতে চাইলে নিচের message অংশটি edit করো
WELCOME_MESSAGE = """👋 Welcome {name}!

🎉 আমাদের গ্রুপে আপনাকে স্বাগতম!
❤️ আশা করি আমাদের সাথে ভালো সময় কাটবে।

📌 গ্রুপের নিয়ম মেনে চলুন।
"""


# =========================
# WELCOME SYSTEM
# =========================

async def welcome_new_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_member = update.chat_member

    old_status = chat_member.old_chat_member.status
    new_status = chat_member.new_chat_member.status

    # নতুন member join করেছে কিনা
    joined = (
        new_status in ["member", "administrator"]
        and old_status in ["left", "kicked"]
    )

    if not joined:
        return

    user = chat_member.new_chat_member.user
    name = user.first_name

    message = WELCOME_MESSAGE.format(name=name)

    await context.bot.send_message(
        chat_id=chat_member.chat.id,
        text=message
    )


# =========================
# START BOT
# =========================

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ প্রথমে BOT_TOKEN বসাও!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(
        ChatMemberHandler(
            welcome_new_member,
            ChatMemberHandler.CHAT_MEMBER
        )
    )

    print("🤖 Welcome Bot চালু হয়েছে!")
    print("👥 এখন Group-এর নতুন member-কে Welcome করবে...")

    app.run_polling()


if __name__ == "__main__":
    main()
