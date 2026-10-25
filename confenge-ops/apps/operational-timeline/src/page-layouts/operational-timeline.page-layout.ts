import {
  definePageLayout,
  PageLayoutTabLayoutMode,
  PageLayoutWidgetVerticalListHeightBehavior,
} from 'twenty-sdk/define';

import {
  APP_DISPLAY_NAME,
  TIMELINE_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
  TIMELINE_PAGE_LAYOUT_TAB_UNIVERSAL_IDENTIFIER,
  TIMELINE_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
  TIMELINE_WIDGET_UNIVERSAL_IDENTIFIER,
} from 'src/constants/universal-identifiers';

export default definePageLayout({
  universalIdentifier: TIMELINE_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
  name: APP_DISPLAY_NAME,
  type: 'STANDALONE_PAGE',
  tabs: [
    {
      universalIdentifier: TIMELINE_PAGE_LAYOUT_TAB_UNIVERSAL_IDENTIFIER,
      title: 'Marcos contratuais',
      position: 0,
      icon: 'IconTimeline',
      layoutMode: PageLayoutTabLayoutMode.VERTICAL_LIST,
      widgets: [
        {
          universalIdentifier: TIMELINE_WIDGET_UNIVERSAL_IDENTIFIER,
          title: ' ',
          type: 'FRONT_COMPONENT',
          heightBehavior:
            PageLayoutWidgetVerticalListHeightBehavior.TAB_VIEWPORT,
          configuration: {
            configurationType: 'FRONT_COMPONENT',
            frontComponentUniversalIdentifier:
              TIMELINE_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
          },
        },
      ],
    },
  ],
});
