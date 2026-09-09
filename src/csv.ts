/**
 * Minimal RFC 4180 CSV reader for GTFS text files.
 *
 * GTFS quotes any field containing a comma (`"Mt. Holly Road"`), so splitting
 * on commas is wrong. This handles quoted fields, escaped `""`, CRLF, and a
 * UTF-8 BOM, which is all the GTFS spec permits.
 */

function splitLine(line: string): string[] {
  const fields: string[] = [];
  let field = '';
  let quoted = false;

  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];
    if (quoted) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      fields.push(field.trim());
      field = '';
    } else {
      field += char;
    }
  }
  fields.push(field.trim());
  return fields;
}

/**
 * Parses CSV text into row objects keyed by header name.
 * Rows with fewer fields than the header yield `undefined` for missing columns.
 */
export function parseCsv(text: string): Array<Record<string, string | undefined>> {
  const clean = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  const lines = clean.split(/\r?\n/).filter((line) => line.length > 0);
  const headerLine = lines[0];
  if (headerLine === undefined) return [];

  const header = splitLine(headerLine);
  const rows: Array<Record<string, string | undefined>> = [];

  for (let i = 1; i < lines.length; i += 1) {
    const raw = lines[i];
    if (raw === undefined) continue;
    const values = splitLine(raw);
    const row: Record<string, string | undefined> = {};
    for (let column = 0; column < header.length; column += 1) {
      const key = header[column];
      if (key === undefined || key === '') continue;
      const value = values[column];
      row[key] = value === '' ? undefined : value;
    }
    rows.push(row);
  }
  return rows;
}
