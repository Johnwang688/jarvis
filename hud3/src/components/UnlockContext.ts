import { createContext, useContext } from "react";

export const UnlockContext = createContext({
  locked: false,
  busy: false,
  unlock: () => {},
});

export function useUnlock() {
  return useContext(UnlockContext);
}
