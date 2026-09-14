import type { InstalledIntegration } from "../../../entities/integration";
import type { RuntimeClient } from "../../../shared/api";

/**
 * Install a plugin, change who may use a service, replace a kept token.
 *
 * An install sends the plugin's id and the values for its own settings; the
 * command that runs is the catalog's and is not something this can send. A
 * secret goes in and is never read back.
 */

export async function installPlugin(
  client: RuntimeClient,
  pluginId: string,
  values: Record<string, string>,
  employees: string[],
): Promise<InstalledIntegration> {
  return client.post<InstalledIntegration>(`/api/plugins/${pluginId}/install`, {
    values,
    employees,
  });
}

export async function grantIntegration(
  client: RuntimeClient,
  integrationId: string,
  employees: string[],
): Promise<InstalledIntegration> {
  return client.put<InstalledIntegration>(`/api/integrations/${integrationId}/grants`, {
    employees,
  });
}

/**
 * Ask a plugin to sign in. False means it opened its own sign-in page on this
 * machine; asking again once that is done answers true. Nothing about the
 * page or the server's reply reaches this window.
 */
export async function signInIntegration(
  client: RuntimeClient,
  integrationId: string,
): Promise<boolean> {
  const body = await client.post<{ signed_in: boolean }>(
    `/api/integrations/${integrationId}/sign-in`,
  );
  return body.signed_in;
}

export async function replaceSecret(
  client: RuntimeClient,
  name: string,
  value: string,
): Promise<void> {
  await client.post("/api/credentials", { name, value });
}
