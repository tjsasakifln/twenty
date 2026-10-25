import {
  defineNavigationMenuItem,
  NavigationMenuItemType,
} from 'twenty-sdk/define';

import {
  APP_DISPLAY_NAME,
  TIMELINE_NAVIGATION_UNIVERSAL_IDENTIFIER,
  TIMELINE_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
} from 'src/constants/universal-identifiers';

export default defineNavigationMenuItem({
  universalIdentifier: TIMELINE_NAVIGATION_UNIVERSAL_IDENTIFIER,
  name: APP_DISPLAY_NAME,
  icon: 'IconTimeline',
  position: -1997.6,
  type: NavigationMenuItemType.PAGE_LAYOUT,
  pageLayoutUniversalIdentifier: TIMELINE_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
});
