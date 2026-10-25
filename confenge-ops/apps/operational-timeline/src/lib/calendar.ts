import {
  BRT_TIME_ZONE,
  type TimelineDate,
  type TimelineTask,
} from 'src/lib/timeline';

const DAY_MS = 86_400_000;

export type CalendarEvent = {
  task: TimelineTask;
  date: TimelineDate;
  allDay: boolean;
};

export const toCalendarEvent = (task: TimelineTask): CalendarEvent | null => {
  const date = task.end;

  return date
    ? {
        task,
        date,
        allDay: date.kind === 'DATE',
      }
    : null;
};

export const dateFromDay = (day: number) => new Date(day * DAY_MS);

export const dayFromUtcParts = (year: number, monthIndex: number, day: number) =>
  Math.floor(Date.UTC(year, monthIndex, day) / DAY_MS);

export const currentBrtDay = () => {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      timeZone: BRT_TIME_ZONE,
    })
      .formatToParts(new Date())
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value]),
  );

  return dayFromUtcParts(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
  );
};

export const firstDayOfMonth = (day: number) => {
  const date = dateFromDay(day);
  return dayFromUtcParts(date.getUTCFullYear(), date.getUTCMonth(), 1);
};

export const addMonths = (day: number, amount: number) => {
  const date = dateFromDay(day);
  return dayFromUtcParts(date.getUTCFullYear(), date.getUTCMonth() + amount, 1);
};

export const startOfWeek = (day: number) => {
  const weekday = dateFromDay(day).getUTCDay();
  const daysSinceMonday = weekday === 0 ? 6 : weekday - 1;
  return day - daysSinceMonday;
};

export const monthGridDays = (anchorDay: number) => {
  const start = startOfWeek(firstDayOfMonth(anchorDay));
  return Array.from({ length: 42 }, (_, index) => start + index);
};

export const weekDays = (anchorDay: number) => {
  const start = startOfWeek(anchorDay);
  return Array.from({ length: 7 }, (_, index) => start + index);
};

export const isSameMonth = (leftDay: number, rightDay: number) => {
  const left = dateFromDay(leftDay);
  const right = dateFromDay(rightDay);
  return (
    left.getUTCFullYear() === right.getUTCFullYear() &&
    left.getUTCMonth() === right.getUTCMonth()
  );
};

export const formatMonth = (day: number) =>
  new Intl.DateTimeFormat('pt-BR', {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(dateFromDay(day));

export const formatDayHeading = (day: number) =>
  new Intl.DateTimeFormat('pt-BR', {
    weekday: 'short',
    day: '2-digit',
    month: 'short',
    timeZone: 'UTC',
  }).format(dateFromDay(day));

export const groupEventsByDay = (events: CalendarEvent[]) => {
  const map = new Map<number, CalendarEvent[]>();

  for (const event of events) {
    map.set(event.date.day, [...(map.get(event.date.day) ?? []), event]);
  }

  for (const grouped of map.values()) {
    grouped.sort((left, right) => {
      if (left.allDay !== right.allDay) {
        return left.allDay ? -1 : 1;
      }

      return left.date.raw.localeCompare(right.date.raw);
    });
  }

  return map;
};
