import {
  defineNavigationMenuItem,
  NavigationMenuItemType,
} from 'twenty-sdk/define';

import {
  CALENDAR_NAVIGATION_UNIVERSAL_IDENTIFIER,
  CALENDAR_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
} from 'src/constants/universal-identifiers';

export default defineNavigationMenuItem({
  universalIdentifier: CALENDAR_NAVIGATION_UNIVERSAL_IDENTIFIER,
  name: 'Agenda operacional',
  icon: 'IconCalendarMonth',
  position: 41,
  type: NavigationMenuItemType.PAGE_LAYOUT,
  pageLayoutUniversalIdentifier: CALENDAR_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
});
