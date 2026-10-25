import { describe, expect, it } from 'vitest';

import {
  dayFromUtcParts,
  groupEventsByDay,
  monthGridDays,
  startOfWeek,
  toCalendarEvent,
  weekDays,
} from 'src/lib/calendar';
import { toTimelineTask } from 'src/lib/timeline';

describe('calendar event normalization', () => {
  it('deduplica prazo histórico preferindo dueAt à cópia date-only', () => {
    const task = toTimelineTask({
      id: 'task-1',
      title: 'Reunião',
      dueAt: '2026-10-25T15:00:00.000Z',
      clickUpDueOn: '2026-10-25',
    });
    const event = toCalendarEvent(task!);

    expect(event?.date.kind).toBe('DATE_TIME');
    expect(event?.allDay).toBe(false);
  });

  it('mantém prazo date-only como evento de dia inteiro', () => {
    const task = toTimelineTask({
      id: 'task-2',
      title: 'Entrega',
      clickUpDueOn: '2026-10-26',
    });

    expect(toCalendarEvent(task!)?.allDay).toBe(true);
  });

  it('não transforma início sem prazo em compromisso', () => {
    const task = toTimelineTask({
      id: 'task-start-only',
      title: 'Início interno',
      clickUpStartOn: '2026-10-26',
    });

    expect(toCalendarEvent(task!)).toBeNull();
  });

  it('ordena dia inteiro antes dos compromissos com hora', () => {
    const allDay = toCalendarEvent(
      toTimelineTask({ id: '1', title: 'A', clickUpDueOn: '2026-10-26' })!,
    )!;
    const timed = toCalendarEvent(
      toTimelineTask({
        id: '2',
        title: 'B',
        dueAt: '2026-10-26T15:00:00.000Z',
      })!,
    )!;

    expect(groupEventsByDay([timed, allDay]).get(allDay.date.day)).toEqual([
      allDay,
      timed,
    ]);
  });
});

describe('calendar periods', () => {
  it('inicia a semana na segunda-feira', () => {
    const sunday = dayFromUtcParts(2026, 9, 25);
    const monday = dayFromUtcParts(2026, 9, 19);

    expect(startOfWeek(sunday)).toBe(monday);
    expect(weekDays(sunday)).toEqual([
      monday,
      monday + 1,
      monday + 2,
      monday + 3,
      monday + 4,
      monday + 5,
      monday + 6,
    ]);
  });

  it('produz grade mensal estável de seis semanas', () => {
    const october = dayFromUtcParts(2026, 9, 15);
    const days = monthGridDays(october);

    expect(days).toHaveLength(42);
    expect(days[0]).toBe(dayFromUtcParts(2026, 8, 28));
    expect(days[41]).toBe(dayFromUtcParts(2026, 10, 8));
  });
});
