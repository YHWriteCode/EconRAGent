import type { PropsWithChildren, ReactElement } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { render } from "@testing-library/react";

import { createQueryClient } from "../app/queryClient";

interface RenderWithProvidersOptions {
  route?: string;
}

function Providers({
  children,
  route = "/",
}: PropsWithChildren<RenderWithProvidersOptions>) {
  const queryClient = createQueryClient();
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

export function renderWithProviders(
  ui: ReactElement,
  options?: RenderWithProvidersOptions,
) {
  return render(ui, {
    wrapper: ({ children }) => (
      <Providers route={options?.route}>{children}</Providers>
    ),
  });
}
