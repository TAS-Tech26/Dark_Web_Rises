import { getApiBaseUrl } from "./dwr-protocol";

/**
 * The backend sends images either as a raw SVG document, a data URI,
 * an http(s) URL, a server-relative path (e.g. /static/images/x.png)
 * or a bare base64 payload. Normalise them all to something an
 * <img src> can render.
 */
export function toImageSrc(image: string | null | undefined): string | undefined {
  if (!image) return undefined;
  const value = image.trim();
  if (value === "") return undefined;

  if (value.startsWith("data:") || value.startsWith("http://") || value.startsWith("https://")) {
    return value;
  }
  // Server-relative path: it lives on the backend, not on the frontend origin.
  if (value.startsWith("/")) return `${getApiBaseUrl().replace(/\/$/, "")}${value}`;
  if (value.startsWith("<svg") || value.startsWith("<?xml")) {
    return `data:image/svg+xml;utf8,${encodeURIComponent(value)}`;
  }
  return `data:image/png;base64,${value}`;
}

