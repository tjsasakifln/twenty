import { defineFrontComponent } from 'twenty-sdk/define';
import { AppPath, navigate } from 'twenty-sdk/front-component';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTheme } from 'twenty-ui/theme-constants';

import {
  APP_DESCRIPTION,
  APP_DISPLAY_NAME,
  TIMELINE_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
  VIEW_ID_VARIABLE,
} from 'src/constants/universal-identifiers';
import {
  groupTimelineLinksByTask,
  loadTimelinePayload,
  type TimelineDependency,
  type TimelinePayload,
} from 'src/lib/timeline-api';
import {
  buildTimelineRange,
  formatRangeDay,
  positionForDay,
  sortTimelineTasks,
  toTimelineTask,
  type TimelineTask,
} from 'src/lib/timeline';

const themePalette = (theme: ReturnType<typeof useTheme>) => ({
  ink: theme.font.color.primary,
  muted: theme.font.color.secondary,
  line: theme.border.color.light,
  panel: theme.background.primary,
  canvas: theme.background.secondary,
  accent: theme.accent.primary,
  accentSoft: theme.background.transparent.blue,
  milestone: theme.accent.secondary,
  danger: theme.font.color.danger,
});

const safeMessage = (error: unknown) =>
  error instanceof Error ? error.message : 'Não foi possível carregar o cronograma.';

const dependencyLabel = (
  dependency: TimelineDependency,
  titleById: Map<string, string>,
) => {
  const from = titleById.get(dependency.fromTaskId) ?? 'Tarefa não visível';
  const to = titleById.get(dependency.toTaskId) ?? 'Tarefa não visível';
  return `${from} depende de ${to}`;
};

const relatedLabel = (
  dependency: TimelineDependency,
  currentTaskId: string,
  titleById: Map<string, string>,
) => {
  const otherTaskId =
    dependency.fromTaskId === currentTaskId
      ? dependency.toTaskId
      : dependency.fromTaskId;
  return `Relacionada a ${titleById.get(otherTaskId) ?? 'tarefa não visível'}`;
};

const OperationalTimeline = () => {
  const theme = useTheme();
  const palette = themePalette(theme);
  const buttonStyle = {
    border: `1px solid ${palette.line}`,
    borderRadius: theme.border.radius.sm,
    background: palette.panel,
    color: palette.ink,
    cursor: 'pointer',
    fontSize: 12,
    fontWeight: 600,
    minHeight: 32,
    padding: '6px 10px',
  } as const;
  const [payload, setPayload] = useState<TimelinePayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('');
  const [showUndated, setShowUndated] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      setPayload(await loadTimelinePayload({ viewVariableName: VIEW_ID_VARIABLE }));
    } catch (caught) {
      setError(safeMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const tasks = useMemo(
    () =>
      sortTimelineTasks(
        (payload?.tasks ?? [])
          .map(toTimelineTask)
          .filter((task): task is TimelineTask => task !== null),
      ),
    [payload],
  );
  const statuses = useMemo(
    () =>
      [...new Set(tasks.map((task) => task.status).filter(Boolean) as string[])].sort(
        (left, right) => left.localeCompare(right, 'pt-BR'),
      ),
    [tasks],
  );
  const visibleTasks = useMemo(
    () =>
      tasks.filter(
        (task) =>
          (!statusFilter || task.status === statusFilter) &&
          (showUndated || task.kind !== 'UNDATED'),
      ),
    [showUndated, statusFilter, tasks],
  );
  const range = useMemo(() => buildTimelineRange(visibleTasks), [visibleTasks]);
  const titleById = useMemo(
    () => new Map(tasks.map((task) => [task.id, task.title])),
    [tasks],
  );
  const parentById = useMemo(
    () => new Map(tasks.map((task) => [task.id, task.parentId])),
    [tasks],
  );
  const linksByTask = useMemo(
    () => groupTimelineLinksByTask(payload?.dependencies ?? []),
    [payload],
  );

  const taskDepth = (task: TimelineTask) => {
    let depth = 0;
    let cursor = task.parentId;
    const seen = new Set<string>();

    while (cursor && depth < 3 && !seen.has(cursor)) {
      seen.add(cursor);
      depth += 1;
      cursor = parentById.get(cursor) ?? null;
    }

    return depth;
  };

  const openTask = (taskId: string) =>
    navigate(AppPath.RecordShowPage, {
      objectNameSingular: 'task',
      objectRecordId: taskId,
    });

  if (loading) {
    return (
      <div style={{ padding: 32, color: palette.muted }} role="status">
        Carregando tarefas, datas e dependências…
      </div>
    );
  }

  if (error) {
    return (
      <div
        style={{
          padding: 32,
          color: palette.ink,
          fontFamily: 'Inter, system-ui, sans-serif',
        }}
      >
        <h2 style={{ margin: '0 0 8px' }}>Cronograma indisponível</h2>
        <p role="alert" style={{ color: palette.danger, maxWidth: 680 }}>
          {error}
        </p>
        <button type="button" onClick={() => void refresh()} style={buttonStyle}>
          Tentar novamente
        </button>
      </div>
    );
  }

  return (
    <main
      style={{
        background: palette.canvas,
        color: palette.ink,
        display: 'flex',
        flexDirection: 'column',
        fontFamily: 'Inter, system-ui, sans-serif',
        height: '100%',
        minHeight: 560,
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          alignItems: 'center',
          background: palette.panel,
          borderBottom: `1px solid ${palette.line}`,
          display: 'flex',
          flexWrap: 'wrap',
          gap: 12,
          justifyContent: 'space-between',
          padding: '14px 18px',
        }}
      >
        <div>
          <h1 style={{ fontSize: 18, margin: 0 }}>{APP_DISPLAY_NAME}</h1>
          <div style={{ color: palette.muted, fontSize: 12, marginTop: 3 }}>
            Filtro nativo: {payload?.viewName} · {visibleTasks.length} de{' '}
            {tasks.length} tarefas
          </div>
        </div>
        <div style={{ alignItems: 'center', display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          <label style={{ color: palette.muted, fontSize: 12 }}>
            Status{' '}
            <select
              aria-label="Filtrar por status"
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value)}
              style={{ ...buttonStyle, cursor: 'default', marginLeft: 4 }}
            >
              <option value="">Todos</option>
              {statuses.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
          <label style={{ color: palette.muted, fontSize: 12 }}>
            <input
              type="checkbox"
              checked={showUndated}
              onChange={(event) => setShowUndated(event.target.checked)}
            />{' '}
            Sem data
          </label>
          <button type="button" onClick={() => void refresh()} style={buttonStyle}>
            Atualizar
          </button>
        </div>
      </header>

      <section
        aria-label="Cronograma de marcos contratuais"
        style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 16 }}
      >
        {visibleTasks.length === 0 ? (
          <div
            style={{
              background: palette.panel,
              border: `1px solid ${palette.line}`,
              borderRadius: 10,
              color: palette.muted,
              padding: 24,
            }}
          >
            Nenhuma tarefa corresponde aos filtros atuais.
          </div>
        ) : (
          <div
            role="table"
            aria-label="Tarefas no tempo"
            style={{
              background: palette.panel,
              border: `1px solid ${palette.line}`,
              borderRadius: 10,
              minWidth: 900,
              overflow: 'hidden',
            }}
          >
            <div
              role="row"
              style={{
                display: 'grid',
                gridTemplateColumns: '330px minmax(560px, 1fr)',
                minHeight: 54,
              }}
            >
              <div
                role="columnheader"
                style={{
                  alignItems: 'center',
                  borderRight: `1px solid ${palette.line}`,
                  display: 'flex',
                  fontSize: 12,
                  fontWeight: 700,
                  padding: '0 14px',
                }}
              >
                Tarefa / vínculo
              </div>
              <div
                role="columnheader"
                style={{ padding: '9px 18px', position: 'relative' }}
              >
                {range ? (
                  <div
                    style={{
                      color: palette.muted,
                      display: 'flex',
                      fontSize: 11,
                      justifyContent: 'space-between',
                    }}
                  >
                    <span>{formatRangeDay(range.firstDay)}</span>
                    <span>
                      {formatRangeDay(
                        Math.round((range.firstDay + range.lastDay) / 2),
                      )}
                    </span>
                    <span>{formatRangeDay(range.lastDay)}</span>
                  </div>
                ) : (
                  <span style={{ color: palette.muted, fontSize: 11 }}>
                    Sem datas para posicionar
                  </span>
                )}
              </div>
            </div>

            {visibleTasks.map((task) => {
              const links = linksByTask.get(task.id) ?? {
                dependsOn: [],
                related: [],
              };
              const startDay = task.start?.day;
              const endDay = task.end?.day;
              const invalidInterval =
                startDay !== undefined &&
                endDay !== undefined &&
                endDay < startDay;
              const markerDay = startDay ?? endDay;
              const left =
                range && markerDay !== undefined
                  ? positionForDay(markerDay, range)
                  : 0;
              const width =
                range &&
                startDay !== undefined &&
                endDay !== undefined &&
                !invalidInterval
                  ? Math.max(0.8, positionForDay(endDay, range) - left)
                  : 0;
              const parentTitle = task.parentId
                ? titleById.get(task.parentId)
                : null;

              return (
                <div
                  role="row"
                  key={task.id}
                  style={{
                    borderTop: `1px solid ${palette.line}`,
                    display: 'grid',
                    gridTemplateColumns: '330px minmax(560px, 1fr)',
                    minHeight: 58,
                  }}
                >
                  <div
                    role="cell"
                    style={{
                      borderRight: `1px solid ${palette.line}`,
                      minWidth: 0,
                      padding: `8px 10px 7px ${12 + taskDepth(task) * 16}px`,
                    }}
                  >
                    <button
                      type="button"
                      onClick={() => void openTask(task.id)}
                      aria-label={`Abrir e editar datas de ${task.title}`}
                      title="Abrir tarefa no Twenty para editar datas"
                      style={{
                        background: 'transparent',
                        border: 0,
                        color: palette.ink,
                        cursor: 'pointer',
                        display: 'block',
                        fontSize: 12,
                        fontWeight: 650,
                        maxWidth: '100%',
                        overflow: 'hidden',
                        padding: 0,
                        textAlign: 'left',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {task.title}
                    </button>
                    <div
                      style={{
                        color: invalidInterval ? palette.danger : palette.muted,
                        fontSize: 10,
                        marginTop: 4,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {invalidInterval
                        ? 'Prazo anterior ao início — corrija na tarefa'
                        : parentTitle
                          ? `Subtarefa de ${parentTitle}`
                          : task.status ?? 'Sem status'}
                      {links.dependsOn.length > 0
                        ? ` · depende de ${links.dependsOn.length}`
                        : ''}
                      {links.related.length > 0
                        ? ` · ${links.related.length} relacionada(s)`
                        : ''}
                    </div>
                  </div>
                  <div
                    role="cell"
                    style={{
                      backgroundImage:
                        `linear-gradient(to right, transparent 24.8%, ${palette.line} 25%, transparent 25.2%, transparent 49.8%, ${palette.line} 50%, transparent 50.2%, transparent 74.8%, ${palette.line} 75%, transparent 75.2%)`,
                      minHeight: 58,
                      padding: '0 18px',
                      position: 'relative',
                    }}
                  >
                    {range && task.kind === 'BAR' && !invalidInterval ? (
                      <button
                        type="button"
                        onClick={() => void openTask(task.id)}
                        aria-label={`${task.title}: ${task.start?.label} até ${task.end?.label}. Abrir tarefa.`}
                        title={`${task.start?.label} → ${task.end?.label}`}
                        style={{
                          background: palette.accent,
                          border: 0,
                          borderRadius: 5,
                          cursor: 'pointer',
                          height: 18,
                          left: `${3 + left * 0.94}%`,
                          minWidth: 8,
                          position: 'absolute',
                          top: 12,
                          width: `${Math.min(100 - left, width) * 0.94}%`,
                        }}
                      />
                    ) : null}
                    {range &&
                    markerDay !== undefined &&
                    (task.kind === 'MILESTONE' || invalidInterval) ? (
                      <button
                        type="button"
                        onClick={() => void openTask(task.id)}
                        aria-label={`${task.title}: ${task.start?.label ?? task.end?.label}. Abrir tarefa.`}
                        title={task.start?.label ?? task.end?.label}
                        style={{
                          background: invalidInterval
                            ? palette.danger
                            : palette.milestone,
                          border: `2px solid ${palette.panel}`,
                          boxShadow: '0 0 0 1px currentColor',
                          cursor: 'pointer',
                          height: 14,
                          left: `${3 + left * 0.94}%`,
                          position: 'absolute',
                          top: 14,
                          transform: 'rotate(45deg)',
                          width: 14,
                        }}
                      />
                    ) : null}
                    {links.dependsOn.length + links.related.length > 0 ? (
                      <div
                        aria-label="Dependências da tarefa"
                        style={{
                          bottom: 5,
                          color: palette.muted,
                          fontSize: 9,
                          left: 18,
                          maxWidth: 'calc(100% - 36px)',
                          overflow: 'hidden',
                          position: 'absolute',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                        title={[
                          ...links.dependsOn.map((dependency) =>
                            dependencyLabel(dependency, titleById),
                          ),
                          ...links.related.map((dependency) =>
                            relatedLabel(dependency, task.id, titleById),
                          ),
                        ]
                          .join('\n')}
                      >
                        ↳{' '}
                        {[
                          ...links.dependsOn.map((dependency) =>
                            dependencyLabel(dependency, titleById),
                          ),
                          ...links.related.map((dependency) =>
                            relatedLabel(dependency, task.id, titleById),
                          ),
                        ]
                          .join(' · ')}
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <footer
        style={{
          background: palette.panel,
          borderTop: `1px solid ${palette.line}`,
          color: palette.muted,
          fontSize: 11,
          padding: '9px 18px',
        }}
      >
        Barra = início e prazo · losango = uma única data · clique para abrir a
        tarefa e editar pelos campos nativos. Datas sem hora são preservadas como
        data civil; horários são exibidos em Brasília.
      </footer>
    </main>
  );
};

export default defineFrontComponent({
  universalIdentifier: TIMELINE_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
  name: APP_DISPLAY_NAME,
  description: APP_DESCRIPTION,
  component: OperationalTimeline,
});
