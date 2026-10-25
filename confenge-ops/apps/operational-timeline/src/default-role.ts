import { defineApplicationRole, STANDARD_OBJECT } from 'twenty-sdk/define';

import {
  APP_DISPLAY_NAME,
  DEFAULT_ROLE_UNIVERSAL_IDENTIFIER,
} from 'src/constants/universal-identifiers';

// Custom object identifiers are deployment metadata, not source data. The
// production build supplies clickUpDependency/clickUpList as a comma-separated
// list. A local build remains safe and only grants access to standard Tasks.
const customReadObjectUniversalIdentifiers = (
  process.env.CONFENGE_TIMELINE_READ_OBJECT_UNIVERSAL_IDENTIFIERS ?? ''
)
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean);

export default defineApplicationRole({
  universalIdentifier: DEFAULT_ROLE_UNIVERSAL_IDENTIFIER,
  label: `${APP_DISPLAY_NAME} — operações controladas`,
  description:
    'Lê o cronograma e atualiza somente tarefas pelas automações de consistência; a edição do usuário ocorre na interface nativa.',
  canReadAllObjectRecords: false,
  canUpdateAllObjectRecords: false,
  canSoftDeleteAllObjectRecords: false,
  canDestroyAllObjectRecords: false,
  objectPermissions: [
    {
      objectUniversalIdentifier: STANDARD_OBJECT.task.universalIdentifier,
      canReadObjectRecords: true,
      canUpdateObjectRecords: true,
      canSoftDeleteObjectRecords: false,
      canDestroyObjectRecords: false,
    },
    ...customReadObjectUniversalIdentifiers.map((objectUniversalIdentifier) => ({
      objectUniversalIdentifier,
      canReadObjectRecords: true,
      canUpdateObjectRecords: false,
      canSoftDeleteObjectRecords: false,
      canDestroyObjectRecords: false,
    })),
  ],
});
