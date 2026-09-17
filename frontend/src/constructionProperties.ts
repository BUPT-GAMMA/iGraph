function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
const owns = (value: object, key: string) => Object.prototype.hasOwnProperty.call(value, key);

/** Business names stay literal, including data, table, dots and nested JSON. */
export function constructionProperties(item: Record<string, unknown> | undefined): Record<string, unknown> {
  if (!item || !record(item.properties)) return {};
  const props = item.properties;
  const row = typeof props.table === 'string' && props.table.length > 0 &&
    Number.isInteger(props.row_index) && Number(props.row_index) >= 0 && record(props.data);
  const relocated = 'source' in item && Array.isArray(props.relocated_attributes) && props.relocated_attributes.length > 0 &&
    props.relocated_attributes.every(value => record(value) &&
      ['row_id', 'column', 'value'].every(key => owns(value, key)) &&
      Object.keys(value).every(key => ['row_id', 'column', 'value', 'name'].includes(key)) &&
      ['row_id', 'column'].every(key => typeof value[key] === 'string' && value[key].length > 0) &&
      (!owns(value, 'name') || typeof value.name === 'string' && value.name.length > 0))
    ? props.relocated_attributes as Array<Record<string, unknown>> : [];
  const entries = new Map(Object.entries(row ? props.data as Record<string, unknown> : props)
    .filter(([key]) => row || !relocated.length || key !== 'relocated_attributes'));
  for (const attribute of relocated) {
    const name = String(attribute.name ?? attribute.column);
    if (!entries.has(name)) entries.set(name, attribute.value);
    else if (JSON.stringify(entries.get(name)) !== JSON.stringify(attribute.value)) {
      // Show conflicts instead of silently choosing an endpoint's value.
      // The server rejects an ambiguous edit with property_conflict.
      entries.set(name, [entries.get(name), attribute.value]);
    }
  }
  return Object.fromEntries(entries);
}
