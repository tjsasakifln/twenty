import { defineApplication, FieldType } from 'twenty-sdk/define';

import {
  APP_DESCRIPTION,
  APP_DISPLAY_NAME,
  APPLICATION_UNIVERSAL_IDENTIFIER,
  CALENDAR_VIEW_ID_VARIABLE,
  CALENDAR_VIEW_ID_VARIABLE_UNIVERSAL_IDENTIFIER,
  CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS,
  VIEW_ID_VARIABLE,
  VIEW_ID_VARIABLE_UNIVERSAL_IDENTIFIER,
} from 'src/constants/universal-identifiers';

const CUTOVER_API_BASE_URL_VARIABLE_UNIVERSAL_IDENTIFIER =
  '8fe34c00-2e68-47bf-ba10-407507bfcd63';

export default defineApplication({
  universalIdentifier: APPLICATION_UNIVERSAL_IDENTIFIER,
  displayName: APP_DISPLAY_NAME,
  description: APP_DESCRIPTION,
  applicationVariables: {
    [VIEW_ID_VARIABLE]: {
      universalIdentifier: VIEW_ID_VARIABLE_UNIVERSAL_IDENTIFIER,
      label: 'ID da visão Marcos contratuais',
      description:
        'ID da visão nativa usada como fonte dos filtros. Configurado apenas no workspace; não pertence ao código-fonte.',
      type: FieldType.TEXT,
      isRequired: true,
    },
    [CALENDAR_VIEW_ID_VARIABLE]: {
      universalIdentifier: CALENDAR_VIEW_ID_VARIABLE_UNIVERSAL_IDENTIFIER,
      label: 'ID da visão fonte da Agenda operacional',
      description:
        'ID da visão nativa usada para filtrar a agenda combinada. Configurado apenas no workspace.',
      type: FieldType.TEXT,
      isRequired: true,
    },
    CUTOVER_ENABLED: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_ENABLED,
      label: 'Ativar automações de cutover',
      description:
        'Mantenha desligado durante instalação e configuração; ative somente depois da validação.',
      type: FieldType.BOOLEAN,
      value: false,
    },
    CUTOVER_API_BASE_URL: {
      universalIdentifier: CUTOVER_API_BASE_URL_VARIABLE_UNIVERSAL_IDENTIFIER,
      label: 'Endpoint interno das automações',
      description:
        'Origem interna allowlisted usada somente pelo runtime das automações no worker.',
      type: FieldType.TEXT,
      isRequired: true,
    },
    CUTOVER_MEMBER_ID: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_MEMBER_ID,
      label: 'Responsável padrão das novas tarefas',
      description: 'ID do workspace member usado somente pelas automações.',
      type: FieldType.TEXT,
      isSecret: true,
      isRequired: true,
    },
    CUTOVER_DEFAULT_LIST_ID: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_DEFAULT_LIST_ID,
      label: 'Lista padrão das novas tarefas',
      description: 'ID da lista operacional padrão.',
      type: FieldType.TEXT,
      isSecret: true,
      isRequired: true,
    },
    CUTOVER_STAGE_STATUS_MAP: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_STAGE_STATUS_MAP,
      label: 'Mapa etapa → status nativo',
      description:
        'Objeto JSON que associa cada opção de Etapa a TODO, IN_PROGRESS ou DONE.',
      type: FieldType.RAW_JSON,
      isSecret: true,
      isRequired: true,
    },
    CUTOVER_STAGE_TODO: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_STAGE_TODO,
      label: 'Etapa padrão para TODO',
      type: FieldType.TEXT,
      isSecret: true,
      isRequired: true,
    },
    CUTOVER_STAGE_IN_PROGRESS: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_STAGE_IN_PROGRESS,
      label: 'Etapa padrão para IN_PROGRESS',
      type: FieldType.TEXT,
      isSecret: true,
      isRequired: true,
    },
    CUTOVER_STAGE_DONE: {
      universalIdentifier:
        CUTOVER_VARIABLE_UNIVERSAL_IDENTIFIERS.CUTOVER_STAGE_DONE,
      label: 'Etapa padrão para DONE',
      type: FieldType.TEXT,
      isSecret: true,
      isRequired: true,
    },
  },
});
