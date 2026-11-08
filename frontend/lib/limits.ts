// Mirrors the server's limits (backend/app/api/chat.py MAX_MESSAGE_CHARS); the server
// enforces them regardless, and these give a clear message before a request is sent.
export const MAX_MESSAGE_CHARS = 2000;
