const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export function getApiUrl() {
  return API_URL
}

export async function fetchJson(path, options = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    },
    ...options
  })

  if (!response.ok) {
    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('application/json')) {
      const payload = await response.json()
      const message = payload?.detail || payload?.message
      throw new Error(message || `Request failed: ${response.status}`)
    }

    const text = await response.text()
    throw new Error(text || `Request failed: ${response.status}`)
  }

  return response.json()
}
