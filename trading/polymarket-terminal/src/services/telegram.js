import dotenv from 'dotenv';
dotenv.config();

const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN || process.env.TELEGRAM_TOKEN || '';
const CHAT_ID = process.env.CHAT_ID || '-1003948211258';
const TOPIC_ID = process.env.TOPIC_ID || '3';
const DRY_RUN = process.env.DRY_RUN !== 'false';

export function notify(message) {
  if (!TELEGRAM_TOKEN) {
    console.log(`[TG-SKIP] No token — ${message}`);
    return;
  }
  const prefix = DRY_RUN ? '🧪 [SIM] ' : '';
  const text = prefix + message;
  const body = {
    chat_id: CHAT_ID,
    text,
    message_thread_id: parseInt(TOPIC_ID),
  };
  fetch(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
    .then(r => {
      if (!r.ok) console.log(`[TG-ERR] ${r.status} ${r.statusText}`);
    })
    .catch(e => console.log(`[TG-ERR] ${e.message}`));
}
