// Thin API client. Errors are surfaced as Error objects with the server's message.
const BASE = import.meta.env.VITE_API_BASE ?? "";

function authHeaders() {
  try {
    const key = localStorage.getItem("di_api_key");
    return key ? { "X-API-Key": key } : {};
  } catch {
    return {}; // storage can be unavailable (private mode); the API key is optional
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}/api${path}`, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) },
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ");
    } catch {
      /* non-JSON error body */
    }
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

const json = (body) => ({ method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const api = {
  upload(files) {
    const form = new FormData();
    for (const f of files) form.append("files", f);
    return request("/documents", { method: "POST", body: form });
  },
  list(params = {}) {
    const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)).toString();
    return request(`/documents${qs ? `?${qs}` : ""}`);
  },
  get: (id) => request(`/documents/${id}`),
  status: (id) => request(`/documents/${id}/status`),
  save: (id, body) => request(`/documents/${id}/extraction`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  reprocess: (id) => request(`/documents/${id}/reprocess`, { method: "POST" }),
  remove: (id) => request(`/documents/${id}`, { method: "DELETE" }),
  search: (q, mode) => request(`/search?${new URLSearchParams({ q, mode })}`),
  stats: () => request("/stats"),
  fileUrl: (id) => `${BASE}/api/documents/${id}/file`,
};

export { json };
