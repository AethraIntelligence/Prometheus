import type { RuntimeClient } from "../../../shared/api";
import type { Employee, EmployeeProfile, Workforce } from "../model/types";

export const employeeApi = {
  async all(client: RuntimeClient): Promise<Employee[]> {
    const body = await client.get<{ employees: Employee[] }>("/api/employees");
    return body.employees;
  },
  workforce(client: RuntimeClient): Promise<Workforce> {
    return client.get<Workforce>("/api/workforce");
  },
  profile(client: RuntimeClient, name: string, windowDays = 30): Promise<EmployeeProfile> {
    return client.get<EmployeeProfile>(
      `/api/workforce/${encodeURIComponent(name)}?window_days=${windowDays}`,
    );
  },
};
