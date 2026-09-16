const configuredApiBaseUrl = String(import.meta.env.VITE_API_BASE_URL || '').trim()

if (!configuredApiBaseUrl) {
  throw new Error('VITE_API_BASE_URL is required. Configure it in the root .env file.')
}

export const API_BASE_URL = configuredApiBaseUrl.replace(/\/+$/, '')

export function apiUrl(path) {
  return `${API_BASE_URL}/${String(path).replace(/^\/+/, '')}`
}
