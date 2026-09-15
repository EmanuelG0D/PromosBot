import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ================= CONFIGURACIÓN =================
TOKEN = "8813450602:AAGvO9LtAsorMA0IS4QPn9Hu5vGZDB8hv6s"
MI_ID = 5583002220
RUTA_PROYECTO = r"C:\Users\Emanuel\Desktop\BotPromos"
RAMA_GIT = "main" # Cambia a "master" si tu repo usa esa rama
# =================================================

async def trabajar_con_agy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Seguridad: Solo tú le puedes dar órdenes
    if update.message.from_user.id != MI_ID:
        return

    orden_natural = update.message.text
    await update.message.reply_text(f"🤖 Copiado bro. Lanzando Antigravity con:\n'{orden_natural}'\n\nTrabajando de fondo...")

    try:
        # Ejecutar Antigravity de fondo
        comando_agy = f'agy "{orden_natural}" --yes' 
        proceso = subprocess.run(
            comando_agy, shell=True, cwd=RUTA_PROYECTO, capture_output=True, text=True
        )

        # Automatización de Git
        subprocess.run('git add .', shell=True, cwd=RUTA_PROYECTO)
        mensaje_commit = f"Ajuste remoto: {orden_natural[:30]}..."
        subprocess.run(f'git commit -m "{mensaje_commit}"', shell=True, cwd=RUTA_PROYECTO)
        subprocess.run(f'git push origin {RAMA_GIT}', shell=True, cwd=RUTA_PROYECTO)

        # Reporte final
        respuesta = "✅ Trabajo terminado y subido al repo.\n\n"
        if proceso.stdout:
            respuesta += f"Log:\n{proceso.stdout[-500:]}"
            
        await update.message.reply_text(respuesta)

    except Exception as e:
        await update.message.reply_text(f"❌ Error bro: {e}")

if __name__ == '__main__':
    print("🔥 Puente remoto activado. Esperando órdenes desde Telegram...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trabajar_con_agy))
    app.run_polling()