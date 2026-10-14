export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function detailText(detail, fallback) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return fallback;
}

async function request(method, path, { query, json, body } = {}) {
  const url = new URL(`/api/v1${path}`, window.location.origin);
  for (const [key, value] of query ?? []) url.searchParams.append(key, value);
  const init = { method, headers: {} };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (body) {
    init.body = body;
  }
  const response = await fetch(url, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(response.status, detailText(payload?.detail, `${response.status} ${response.statusText}`));
  }
  return payload;
}

export const api = {
  status: () => request("GET", "/jobs/status"),
  jobs: (query) => request("GET", "/jobs", { query }),
  patch: (id, changes) => request("PATCH", `/jobs/${id}`, { json: changes }),
  sync: () => request("POST", "/jobs/sync"), // can take minutes: no client-side timeout
  upload: (file) => {
    const form = new FormData();
    form.append("file", file);
    return request("POST", "/cv/upload", { body: form });
  },
};
