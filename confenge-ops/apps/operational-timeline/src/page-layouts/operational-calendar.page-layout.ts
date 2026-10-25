import {
  definePageLayout,
  PageLayoutTabLayoutMode,
  PageLayoutWidgetVerticalListHeightBehavior,
} from 'twenty-sdk/define';

import {
  CALENDAR_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
  CALENDAR_PAGE_LAYOUT_TAB_UNIVERSAL_IDENTIFIER,
  CALENDAR_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
  CALENDAR_WIDGET_UNIVERSAL_IDENTIFIER,
} from 'src/constants/universal-identifiers';

export default definePageLayout({
  universalIdentifier: CALENDAR_PAGE_LAYOUT_UNIVERSAL_IDENTIFIER,
  name: 'Agenda operacional',
  type: 'STANDALONE_PAGE',
  tabs: [
    {
      universalIdentifier: CALENDAR_PAGE_LAYOUT_TAB_UNIVERSAL_IDENTIFIER,
      title: 'Agenda operacional',
      position: 0,
      icon: 'IconCalendarMonth',
      layoutMode: PageLayoutTabLayoutMode.VERTICAL_LIST,
      widgets: [
        {
          universalIdentifier: CALENDAR_WIDGET_UNIVERSAL_IDENTIFIER,
          title: ' ',
          type: 'FRONT_COMPONENT',
          heightBehavior:
            PageLayoutWidgetVerticalListHeightBehavior.TAB_VIEWPORT,
          configuration: {
            configurationType: 'FRONT_COMPONENT',
            frontComponentUniversalIdentifier:
              CALENDAR_FRONT_COMPONENT_UNIVERSAL_IDENTIFIER,
          },
        },
      ],
    },
  ],
});
