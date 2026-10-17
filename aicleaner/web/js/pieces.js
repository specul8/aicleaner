// A file sent to the server in pieces, each base64 in a JSON body (2026-10-17). The encrypted tunnel
// carries every request whole, as text, so a raw upload stream cannot go through it; pieces go the
// same way sealed or not. Used by the claude.ai import and by dropping files on the file tree.
import { postJSON } from './api.js';

export const PIECE = 3 * 1024 * 1024;

export function base64Of(buffer) {
  const bytes = new Uint8Array(buffer);
  let text = '';
  for (let i = 0; i < bytes.length; i += 0x8000) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(text);
}

// Posts `fields` plus {offset, last, data} for each piece, in order; resolves the last answer.
// progress(sent, total) after each piece.
export async function sendInPieces(url, fields, file, progress = () => {}) {
  let answer = null;
  for (let offset = 0; ; offset += PIECE) {
    const last = offset + PIECE >= file.size;
    const data = base64Of(await file.slice(offset, offset + PIECE).arrayBuffer());
    answer = await postJSON(url, { ...fields, offset, last, data });
    progress(Math.min(file.size, offset + PIECE), file.size);
    if (last) return answer;
  }
}
