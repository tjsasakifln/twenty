import { defineFrontComponent } from 'twenty-sdk/define';
import { AppPath, navigate } from 'twenty-sdk/front-component';
import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  CALENDAR_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
  CALENDAR_VIEW_ID_VARIABLE,
} from 'src/constants/universal-identifiers';
import {
  addMonths,
  currentBrtDay,
  dateFromDay,
  formatDayHeading,
  formatMonth,
  groupEventsByDay,
  isSameMonth,
  monthGridDays,
  toCalendarEvent,
  weekDays,
  type CalendarEvent,
} from 'src/lib/calendar';
import { loadTimelinePayload } from 'src/lib/timeline-api';
import { BRT_TIME_ZONE, toTimelineTask } from 'src/lib/timeline';

const colors = {
  ink: '#172033',
  muted: '#617087',
  border: '#dce2ea',
  surface: '#fff',
  canvas: '#f6f8fb',
  accent: '#3659e3',
  allDay: '#8c3aa6',
  today: '#eef2ff',
  danger: '#b42318',
};

const buttonStyle = {
  background: colors.surface,
  border: `1px solid ${colors.border}`,
  borderRadius: 7,
  color: colors.ink,
  cursor: 'pointer',
  fontSize: 12,
  fontWeight: 600,
  minHeight: 32,
  padding: '6px 10px',
} as const;

const weekDayLabels = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'];

const timeLabel = (event: CalendarEvent) => {
  if (event.allDay) {
    return 'Dia inteiro';
  }

  return new Intl.DateTimeFormat('pt-BR', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: BRT_TIME_ZONE,
  }).format(new Date(event.date.raw));
};

const OperationalCalendar = () => {
  const today = useMemo(currentBrtDay, []);
  const [anchorDay, setAnchorDay] = useState(today);
  const [mode, setMode] = useState<'month' | 'week'>('month');
  const [viewName, setViewName] = useState('');
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const payload = await loadTimelinePayload({
        viewVariableName: CALENDAR_VIEW_ID_VARIABLE,
        includeDependencies: false,
      });
      setViewName(payload.viewName);
      setEvents(
        payload.tasks
          .map(toTimelineTask)
          .filter((task) => task !== null)
          .map(toCalendarEvent)
          .filter((event): event is CalendarEvent => event !== null),
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : 'Não foi possível carregar a agenda.',
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const byDay = useMemo(() => groupEventsByDay(events), [events]);
  const days = useMemo(
    () => (mode === 'month' ? monthGridDays(anchorDay) : weekDays(anchorDay)),
    [anchorDay, mode],
  );
  const openTask = (id: string) =>
    navigate(AppPath.RecordShowPage, {
      objectNameSingular: 'task',
      objectRecordId: id,
    });
  const step = (direction: -1 | 1) =>
    setAnchorDay((day) =>
      mode === 'month' ? addMonths(day, direction) : day + direction * 7,
    );

  return (
    <main
      style={{
        background: colors.canvas,
        color: colors.ink,
        display: 'flex',
        flexDirection: 'column',
        fontFamily: 'Inter, system-ui, sans-serif',
        height: '100%',
        minHeight: 580,
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          alignItems: 'center',
          background: colors.surface,
          borderBottom: `1px solid ${colors.border}`,
          display: 'flex',
          flexWrap: 'wrap',
          gap: 12,
          justifyContent: 'space-between',
          padding: '13px 16px',
        }}
      >
        <div>
          <h1 style={{ fontSize: 18, margin: 0 }}>Agenda operacional</h1>
          <div style={{ color: colors.muted, fontSize: 11, marginTop: 3 }}>
            {viewName ? `Filtro nativo: ${viewName} · ` : ''}
            {events.length} compromisso(s), sem duplicar prazo com hora
          </div>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          <button type="button" style={buttonStyle} onClick={() => step(-1)}>
            Anterior
          </button>
          <button type="button" style={buttonStyle} onClick={() => setAnchorDay(today)}>
            Hoje
          </button>
          <button type="button" style={buttonStyle} onClick={() => step(1)}>
            Próximo
          </button>
          <button
            type="button"
            aria-pressed={mode === 'month'}
            style={{
              ...buttonStyle,
              background: mode === 'month' ? colors.accent : colors.surface,
              color: mode === 'month' ? '#fff' : colors.ink,
            }}
            onClick={() => setMode('month')}
          >
            Mês
          </button>
          <button
            type="button"
            aria-pressed={mode === 'week'}
            style={{
              ...buttonStyle,
              background: mode === 'week' ? colors.accent : colors.surface,
              color: mode === 'week' ? '#fff' : colors.ink,
            }}
            onClick={() => setMode('week')}
          >
            Semana
          </button>
          <button type="button" style={buttonStyle} onClick={() => void refresh()}>
            Atualizar
          </button>
        </div>
      </header>

      <section
        aria-label="Agenda combinada"
        style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 16 }}
      >
        <h2 style={{ fontSize: 16, margin: '0 0 12px', textTransform: 'capitalize' }}>
          {mode === 'month'
            ? formatMonth(anchorDay)
            : `${formatDayHeading(days[0])} — ${formatDayHeading(days[6])}`}
        </h2>

        {loading ? (
          <div role="status" style={{ color: colors.muted, padding: 24 }}>
            Carregando agenda…
          </div>
        ) : error ? (
          <div
            role="alert"
            style={{
              background: colors.surface,
              border: `1px solid ${colors.border}`,
              borderRadius: 9,
              color: colors.danger,
              padding: 20,
            }}
          >
            {error}
          </div>
        ) : mode === 'month' ? (
          <div
            role="grid"
            aria-label={formatMonth(anchorDay)}
            style={{
              background: colors.border,
              border: `1px solid ${colors.border}`,
              borderRadius: 9,
              display: 'grid',
              gap: 1,
              gridTemplateColumns: 'repeat(7, minmax(118px, 1fr))',
              minWidth: 826,
              overflow: 'hidden',
            }}
          >
            {weekDayLabels.map((label) => (
              <div
                role="columnheader"
                key={label}
                style={{
                  background: '#f0f3f8',
                  color: colors.muted,
                  fontSize: 11,
                  fontWeight: 700,
                  padding: '8px 9px',
                }}
              >
                {label}
              </div>
            ))}
            {days.map((day) => {
              const dayEvents = byDay.get(day) ?? [];
              const date = dateFromDay(day);

              return (
                <div
                  role="gridcell"
                  key={day}
                  aria-label={`${formatDayHeading(day)}, ${dayEvents.length} eventos`}
                  style={{
                    background: day === today ? colors.today : colors.surface,
                    color: isSameMonth(day, anchorDay)
                      ? colors.ink
                      : '#98a2b3',
                    minHeight: 105,
                    padding: 7,
                  }}
                >
                  <button
                    type="button"
                    onClick={() => {
                      setAnchorDay(day);
                      setMode('week');
                    }}
                    style={{
                      background: 'transparent',
                      border: 0,
                      color: 'inherit',
                      cursor: 'pointer',
                      fontSize: 11,
                      fontWeight: day === today ? 800 : 650,
                      padding: 0,
                    }}
                  >
                    {date.getUTCDate()}
                  </button>
                  {dayEvents.slice(0, 3).map((event) => (
                    <button
                      type="button"
                      key={event.task.id}
                      onClick={() => void openTask(event.task.id)}
                      title={`${timeLabel(event)} — ${event.task.title}`}
                      style={{
                        background: event.allDay ? '#f4e7f8' : '#e7ecff',
                        border: 0,
                        borderLeft: `3px solid ${event.allDay ? colors.allDay : colors.accent}`,
                        borderRadius: 4,
                        color: colors.ink,
                        cursor: 'pointer',
                        display: 'block',
                        fontSize: 9,
                        marginTop: 5,
                        maxWidth: '100%',
                        overflow: 'hidden',
                        padding: '4px 5px',
                        textAlign: 'left',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        width: '100%',
                      }}
                    >
                      {event.allDay ? '' : `${timeLabel(event)} `}
                      {event.task.title}
                    </button>
                  ))}
                  {dayEvents.length > 3 ? (
                    <div style={{ color: colors.muted, fontSize: 9, marginTop: 4 }}>
                      + {dayEvents.length - 3} outro(s)
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : (
          <div
            role="list"
            style={{
              background: colors.surface,
              border: `1px solid ${colors.border}`,
              borderRadius: 9,
              overflow: 'hidden',
            }}
          >
            {days.map((day) => {
              const dayEvents = byDay.get(day) ?? [];

              return (
                <div
                  role="listitem"
                  key={day}
                  style={{
                    background: day === today ? colors.today : colors.surface,
                    borderTop: day === days[0] ? 0 : `1px solid ${colors.border}`,
                    display: 'grid',
                    gridTemplateColumns: '145px 1fr',
                    minHeight: 62,
                  }}
                >
                  <div style={{ fontSize: 12, fontWeight: 700, padding: 12 }}>
                    {formatDayHeading(day)}
                  </div>
                  <div style={{ padding: '7px 10px' }}>
                    {dayEvents.length === 0 ? (
                      <span style={{ color: colors.muted, fontSize: 11 }}>Sem eventos</span>
                    ) : (
                      dayEvents.map((event) => (
                        <button
                          type="button"
                          key={event.task.id}
                          onClick={() => void openTask(event.task.id)}
                          style={{
                            ...buttonStyle,
                            borderLeft: `4px solid ${event.allDay ? colors.allDay : colors.accent}`,
                            display: 'block',
                            marginBottom: 5,
                            textAlign: 'left',
                            width: '100%',
                          }}
                        >
                          <span
                            style={{
                              color: colors.muted,
                              display: 'inline-block',
                              fontWeight: 500,
                              width: 76,
                            }}
                          >
                            {timeLabel(event)}
                          </span>
                          {event.task.title}
                        </button>
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <footer
        style={{
          background: colors.surface,
          borderTop: `1px solid ${colors.border}`,
          color: colors.muted,
          fontSize: 11,
          padding: '9px 16px',
        }}
      >
        Roxo = dia inteiro · azul = horário de Brasília. Quando uma tarefa contém
        as duas representações históricas, o compromisso com hora prevalece uma
        única vez. Clique para editar na tarefa nativa.
      </footer>
    </main>
  );
};

export default defineFrontComponent({
  universalIdentifier: CALENDAR_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
  name: 'Agenda operacional',
  description:
    'Agenda combinada de prazos date-only e compromissos com hora em Brasília.',
  component: OperationalCalendar,
});
