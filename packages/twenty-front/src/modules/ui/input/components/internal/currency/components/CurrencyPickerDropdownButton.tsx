import { t } from '@lingui/core/macro';
import { type KeyboardEvent, useId } from 'react';
import { CurrencyCode } from 'twenty-shared/constants';
import { isDefined } from 'twenty-shared/utils';
import { CurrencyPicker } from 'twenty-ui/components/input';

import { CURRENCY_PICKER_CURRENCIES } from '@/ui/input/components/internal/currency/constants/CurrencyPickerCurrencies';
import { DropdownContent } from '@/ui/layout/dropdown/components/DropdownContent';
import { DropdownRoot } from '@/ui/layout/dropdown/components/DropdownRoot';

type CurrencyPickerDropdownButtonProps = {
  selectedCurrencyCode: string;
  onChange: (currencyCode: string) => void;
};

const keepEnterAwayFromFieldHotkeys = (
  event: KeyboardEvent<HTMLButtonElement>,
) => {
  if (event.key !== 'Enter') {
    return;
  }

  event.stopPropagation();
};

export const CurrencyPickerDropdownButton = ({
  selectedCurrencyCode,
  onChange,
}: CurrencyPickerDropdownButtonProps) => {
  const dropdownId = useId();

  const handleValueChange = (currencyCode: string | null) => {
    if (!isDefined(currencyCode)) {
      return;
    }

    onChange(currencyCode);
  };

  return (
    <DropdownRoot dropdownId={dropdownId} type="picker">
      <CurrencyPicker.Root
        currencies={CURRENCY_PICKER_CURRENCIES}
        value={selectedCurrencyCode}
        onValueChange={handleValueChange}
        selectedFirst
      >
        {({ selectedCurrency }) => {
          const currencyCode = selectedCurrency?.code ?? CurrencyCode.USD;

          return (
            <>
              <CurrencyPicker.Trigger
                aria-label={t`Currency: ${currencyCode}`}
                onKeyDown={keepEnterAwayFromFieldHotkeys}
              >
                {currencyCode}
              </CurrencyPicker.Trigger>
              <DropdownContent
                side="bottom"
                align="start"
                sideOffset={4}
                alignOffset={0}
                aria-label={t`Currency`}
              >
                <CurrencyPicker.Options
                  listLabel={t`Currency`}
                  searchLabel={t`Search`}
                  emptyLabel={t`No results`}
                />
              </DropdownContent>
            </>
          );
        }}
      </CurrencyPicker.Root>
    </DropdownRoot>
  );
};
