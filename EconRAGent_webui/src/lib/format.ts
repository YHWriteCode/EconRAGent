function parseTime(value?: string | number | null): Date | null {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  if (typeof value === "number") {
    return new Date(value < 1_000_000_000_000 ? value * 1000 : value);
  }
  const numeric = Number(value);
  if (/^\d+(\.\d+)?$/.test(value.trim()) && Number.isFinite(numeric)) {
    return new Date(numeric < 1_000_000_000_000 ? numeric * 1000 : numeric);
  }
  return new Date(value);
}

export function formatTime(value?: string | number | null): string {
  const parsed = parseTime(value);
  if (!parsed) {
    return "-";
  }
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatBeijingTime(value?: string | number | null): string {
  const parsed = parseTime(value);
  if (!parsed) {
    return "-";
  }
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return parsed.toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function truncate(value: string, maxLength: number): string {
  const normalized = value.trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 1).trimEnd()}…`;
}

export function toLocalDateTimeInput(value?: string | null): string {
  if (!value) {
    return "";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  const pad = (item: number) => item.toString().padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}T${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
}

export function toIsoDateTime(value: string): string | undefined {
  if (!value.trim()) {
    return undefined;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return undefined;
  }
  return parsed.toISOString();
}
