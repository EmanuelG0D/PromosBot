import subprocess
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TOKEN = "8813450602:AAGvO9LtAsorMA0IS4QPn9Hu5vGZDB8hv6s"
MI_ID = 5583002220
RUTA_PROYECTO = r"C:\Users\Emanuel\Desktop\BotPromos"
RAMA_GIT = "main"

primera_vez = True

async def trabajar_con_agy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global primera_vez
    if update.message.from_user.id != MI_ID:
        return

    orden = update.message.text
    mensaje_estado = await update.message.reply_text("🤖 Gemini pensando y trabajando en el proyecto...")

    try:
        # Si ya hablamos antes, le agregamos -c para continuar la misma conversación
        flag_continue = "" if primera_vez else "-c"
        
        # Ejecutamos con las banderas nativas de Antigravity
        comando = f'cmd /c agy -p "{orden}" {flag_continue} --dangerously-skip-permissions'
        
        proceso = subprocess.run(
            comando,
            shell=True,
            cwd=RUTA_PROYECTO,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )

        primera_vez = False
        respuesta = proceso.stdout.strip()
        error = proceso.stderr.strip()

        texto_salida = respuesta if respuesta else (f"⚠️ Log:\n{error}" if error else "Acción completada sin texto.")

        # Enviamos la respuesta de Gemini al chat
        await mensaje_estado.edit_text(f"🧠 **Gemini dice:**\n\n{texto_salida[:4000]}")

        # Git automático hacia Bitbucket
        subprocess.run('git add .', shell=True, cwd=RUTA_PROYECTO)
        subprocess.run('git commit -m "Ajuste remoto vía agy"', shell=True, cwd=RUTA_PROYECTO)
        subprocess.run(f'git push origin {RAMA_GIT}', shell=True, cwd=RUTA_PROYECTO)

    except Exception as e:
        await update.message.reply_text(f"❌ Error en el puente: {e}")

if __name__ == '__main__':
    print("🔥 Puente definitivo activado con soporte nativo de agy...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trabajar_con_agy))
    app.run_polling()