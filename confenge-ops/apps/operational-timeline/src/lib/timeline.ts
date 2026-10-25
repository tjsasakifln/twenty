export const BRT_TIME_ZONE = 'America/Sao_Paulo';
const DAY_MS = 86_400_000;

export type UnknownRecord = Record<string, unknown>;

export type ViewFilterLike = {
  id: string;
  fieldMetadataId: string;
  operand: string;
  value: unknown;
  viewFilterGroupId?: string | null;
  subFieldName?: string | null;
};

export type ViewFilterGroupLike = {
  id: string;
  parentViewFilterGroupId?: string | null;
  logicalOperator: 'AND' | 'OR' | 'NOT' | string;
};

export type FieldMetadataLike = {
  id: string;
  name: string;
  type?: string;
  options?: unknown;
};

export type TimelineDate = {
  raw: string;
  day: number;
  kind: 'DATE' | 'DATE_TIME';
  label: string;
};

export type TimelineTask = {
  id: string;
  title: string;
  status: string | null;
  parentId: string | null;
  start: TimelineDate | null;
  end: TimelineDate | null;
  kind: 'BAR' | 'MILESTONE' | 'UNDATED';
  source: UnknownRecord;
};

export type TimelineRange = {
  firstDay: number;
  lastDay: number;
  span: number;
};

const calendarDatePattern = /^(\d{4})-(\d{2})-(\d{2})$/;

const dayFromParts = (year: number, month: number, day: number) =>
  Math.floor(Date.UTC(year, month - 1, day) / DAY_MS);

const formatCalendarDay = (year: number, month: number, day: number) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: BRT_TIME_ZONE,
  }).format(new Date(Date.UTC(year, month - 1, day, 15)));

const dateTimePartsFormatter = new Intl.DateTimeFormat('en-CA', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  timeZone: BRT_TIME_ZONE,
});

const dateTimeLabelFormatter = new Intl.DateTimeFormat('pt-BR', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: BRT_TIME_ZONE,
});

export const parseTimelineDate = (
  value: unknown,
): TimelineDate | null => {
  if (typeof value !== 'string' || value.length === 0) {
    return null;
  }

  const dateMatch = calendarDatePattern.exec(value);

  if (dateMatch) {
    const year = Number(dateMatch[1]);
    const month = Number(dateMatch[2]);
    const day = Number(dateMatch[3]);
    const validationDate = new Date(Date.UTC(year, month - 1, day));

    if (
      validationDate.getUTCFullYear() !== year ||
      validationDate.getUTCMonth() !== month - 1 ||
      validationDate.getUTCDate() !== day
    ) {
      return null;
    }

    return {
      raw: value,
      day: dayFromParts(year, month, day),
      kind: 'DATE',
      label: formatCalendarDay(year, month, day),
    };
  }

  const instant = new Date(value);

  if (Number.isNaN(instant.getTime())) {
    return null;
  }

  const parts = Object.fromEntries(
    dateTimePartsFormatter
      .formatToParts(instant)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  );

  return {
    raw: value,
    day: dayFromParts(
      Number(parts.year),
      Number(parts.month),
      Number(parts.day),
    ),
    kind: 'DATE_TIME',
    label: dateTimeLabelFormatter.format(instant),
  };
};

const firstDate = (record: UnknownRecord, fieldNames: string[]) => {
  for (const fieldName of fieldNames) {
    const parsed = parseTimelineDate(record[fieldName]);

    if (parsed) {
      return parsed;
    }
  }

  return null;
};

const readString = (value: unknown): string | null => {
  if (typeof value === 'string') {
    return value;
  }

  if (value && typeof value === 'object') {
    const record = value as UnknownRecord;
    const candidate = record.label ?? record.name ?? record.value;
    return typeof candidate === 'string' ? candidate : null;
  }

  return null;
};

export const toTimelineTask = (record: UnknownRecord): TimelineTask | null => {
  const id = readString(record.id);

  if (!id) {
    return null;
  }

  const start = firstDate(record, [
    'clickUpStartAt',
    'startAt',
    'startsAt',
    'clickUpStartOn',
    'startOn',
  ]);
  const end = firstDate(record, [
    'dueAt',
    'clickUpDueAt',
    'clickUpDueOn',
    'dueOn',
  ]);
  const parentId =
    readString(record.clickUpParentTaskId) ??
    readString(record.parentTaskId) ??
    readString(record.parentId) ??
    (record.clickUpParentTask &&
    typeof record.clickUpParentTask === 'object'
      ? readString((record.clickUpParentTask as UnknownRecord).id)
      : null);

  return {
    id,
    title: readString(record.title) ?? readString(record.name) ?? 'Sem título',
    status:
      readString(record.clickUpStatus) ?? readString(record.status) ?? null,
    parentId,
    start,
    end,
    kind: start && end ? 'BAR' : start || end ? 'MILESTONE' : 'UNDATED',
    source: record,
  };
};

const parseExpectedValues = (value: unknown): unknown[] => {
  if (Array.isArray(value)) {
    return value.flatMap(parseExpectedValues);
  }

  if (typeof value === 'string') {
    const trimmed = value.trim();

    if (
      (trimmed.startsWith('[') && trimmed.endsWith(']')) ||
      (trimmed.startsWith('{') && trimmed.endsWith('}'))
    ) {
      try {
        return parseExpectedValues(JSON.parse(trimmed));
      } catch {
        return [value];
      }
    }
  }

  if (value && typeof value === 'object') {
    return Object.values(value as UnknownRecord).flatMap(parseExpectedValues);
  }

  return [value];
};

const comparableValues = (value: unknown): string[] =>
  parseExpectedValues(value)
    .filter((entry) => entry !== null && entry !== undefined)
    .map((entry) => String(entry).trim().toLocaleLowerCase('pt-BR'));

const relationExpectedValues = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    const values = value.flatMap((entry) => relationExpectedValues(entry));

    if (values.length === 0) {
      throw new Error('O filtro relacional não contém registros selecionados.');
    }

    return values;
  }

  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as UnknownRecord;

    if (record.isCurrentRecordSelected === true) {
      throw new Error(
        'O filtro relacional “registro atual” não é suportado nesta página independente.',
      );
    }

    if (Array.isArray(record.selectedRecordIds)) {
      const values = comparableValues(record.selectedRecordIds).filter(
        (entry) => entry !== '',
      );

      if (values.length === 0) {
        throw new Error('O filtro relacional não contém registros selecionados.');
      }

      return values;
    }

    if (typeof record.id === 'string') {
      return comparableValues(record.id);
    }

    throw new Error('O filtro relacional possui formato inválido.');
  }

  if (typeof value === 'string') {
    const trimmed = value.trim();

    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        return relationExpectedValues(JSON.parse(trimmed));
      } catch {
        throw new Error('O filtro relacional contém JSON inválido.');
      }
    }

    if (trimmed.length > 0) {
      return comparableValues(trimmed);
    }
  }

  throw new Error('O filtro relacional possui valor inválido.');
};

const relationRecordValues = (
  record: UnknownRecord,
  fieldName: string,
  subFieldName?: string | null,
) => {
  const values: unknown[] = [record[`${fieldName}Id`]];
  const relationValue = record[fieldName];
  const relatedRecords = Array.isArray(relationValue)
    ? relationValue
    : relationValue
      ? [relationValue]
      : [];

  for (const related of relatedRecords) {
    if (!related || typeof related !== 'object') {
      continue;
    }

    const relatedRecord = related as UnknownRecord;
    values.push(subFieldName ? relatedRecord[subFieldName] : relatedRecord.id);
  }

  return comparableValues(values);
};

const recordFieldValues = (
  record: UnknownRecord,
  fieldName: string,
  subFieldName?: string | null,
) => {
  const values = [record[fieldName], record[`${fieldName}Id`]];

  if (subFieldName) {
    const relationValue = record[fieldName];

    if (relationValue && typeof relationValue === 'object') {
      if (Array.isArray(relationValue)) {
        values.push(
          ...relationValue.map((entry) =>
            entry && typeof entry === 'object'
              ? (entry as UnknownRecord)[subFieldName]
              : undefined,
          ),
        );
      } else {
        values.push((relationValue as UnknownRecord)[subFieldName]);
      }
    }
  }

  return comparableValues(values);
};

export const evaluateFilter = (
  record: UnknownRecord,
  filter: ViewFilterLike,
  fieldName: string,
  fieldType?: string,
) => {
  const isRelation = fieldType === 'RELATION';
  const actual = isRelation
    ? relationRecordValues(record, fieldName, filter.subFieldName)
    : recordFieldValues(record, fieldName, filter.subFieldName);
  const expected = isRelation
    ? relationExpectedValues(filter.value)
    : comparableValues(filter.value);
  const equalsAny = actual.some((value) => expected.includes(value));
  const containsAny = actual.some((value) =>
    expected.some((needle) => value.includes(needle)),
  );

  switch (filter.operand) {
    case 'IS':
      return equalsAny;
    case 'IS_NOT':
      return !equalsAny;
    case 'CONTAINS':
      return containsAny;
    case 'DOES_NOT_CONTAIN':
      return !containsAny;
    case 'IS_EMPTY':
    case 'IS_NOT_NULL':
      return filter.operand === 'IS_EMPTY'
        ? actual.length === 0 || actual.every((value) => value === '')
        : actual.some((value) => value !== '');
    case 'IS_NOT_EMPTY':
      return actual.some((value) => value !== '');
    default:
      throw new Error(
        `O filtro ${filter.operand} ainda não é suportado pelo cronograma.`,
      );
  }
};

export const applyViewFilters = (
  records: UnknownRecord[],
  fields: FieldMetadataLike[],
  filters: ViewFilterLike[],
  groups: ViewFilterGroupLike[] = [],
) => {
  const fieldById = new Map(fields.map((field) => [field.id, field]));
  const groupById = new Map<string, ViewFilterGroupLike>();

  for (const group of groups) {
    if (groupById.has(group.id)) {
      throw new Error('A visão contém grupos de filtro duplicados.');
    }

    groupById.set(group.id, group);
  }

  for (const filter of filters) {
    if (filter.viewFilterGroupId && !groupById.has(filter.viewFilterGroupId)) {
      throw new Error('A visão contém um filtro ligado a um grupo inexistente.');
    }
  }

  for (const group of groups) {
    if (
      group.parentViewFilterGroupId &&
      !groupById.has(group.parentViewFilterGroupId)
    ) {
      throw new Error('A visão contém um grupo de filtro órfão.');
    }

    const visited = new Set<string>();
    let cursor: ViewFilterGroupLike | undefined = group;

    while (cursor) {
      if (visited.has(cursor.id)) {
        throw new Error('A visão contém um ciclo entre grupos de filtro.');
      }

      visited.add(cursor.id);
      cursor = cursor.parentViewFilterGroupId
        ? groupById.get(cursor.parentViewFilterGroupId)
        : undefined;
    }
  }

  const filtersByGroup = new Map<string | null, ViewFilterLike[]>();
  const childGroups = new Map<string | null, ViewFilterGroupLike[]>();

  for (const filter of filters) {
    const groupId = filter.viewFilterGroupId ?? null;
    filtersByGroup.set(groupId, [
      ...(filtersByGroup.get(groupId) ?? []),
      filter,
    ]);
  }

  for (const group of groups) {
    const parentId = group.parentViewFilterGroupId ?? null;
    childGroups.set(parentId, [...(childGroups.get(parentId) ?? []), group]);
  }

  const matchesGroup = (
    record: UnknownRecord,
    groupId: string | null,
    operator: string,
  ): boolean => {
    const own = (filtersByGroup.get(groupId) ?? []).map((filter) => {
      const field = fieldById.get(filter.fieldMetadataId);

      if (!field) {
        throw new Error(
          'A visão contém um filtro cujo campo não existe mais nas tarefas.',
        );
      }

      return evaluateFilter(record, filter, field.name, field.type);
    });
    const nested = (childGroups.get(groupId) ?? []).map((child) =>
      matchesGroup(record, child.id, child.logicalOperator),
    );
    const results = [...own, ...nested];

    if (results.length === 0) {
      return true;
    }

    if (operator === 'OR') {
      return results.some(Boolean);
    }

    if (operator === 'NOT') {
      return !results.every(Boolean);
    }

    return results.every(Boolean);
  };

  return records.filter((record) => matchesGroup(record, null, 'AND'));
};

export const buildTimelineRange = (tasks: TimelineTask[]): TimelineRange | null => {
  const days = tasks.flatMap((task) =>
    [task.start?.day, task.end?.day].filter(
      (day): day is number => day !== undefined,
    ),
  );

  if (days.length === 0) {
    return null;
  }

  const firstDay = Math.min(...days);
  const lastDay = Math.max(...days);

  return {
    firstDay,
    lastDay,
    span: Math.max(1, lastDay - firstDay),
  };
};

export const positionForDay = (day: number, range: TimelineRange) =>
  ((day - range.firstDay) / range.span) * 100;

export const formatRangeDay = (day: number) => {
  const date = new Date(day * DAY_MS);

  return new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(date);
};

export const sortTimelineTasks = (tasks: TimelineTask[]) =>
  [...tasks].sort((left, right) => {
    const leftDay = left.start?.day ?? left.end?.day ?? Number.MAX_SAFE_INTEGER;
    const rightDay =
      right.start?.day ?? right.end?.day ?? Number.MAX_SAFE_INTEGER;

    return leftDay - rightDay || left.title.localeCompare(right.title, 'pt-BR');
  });
