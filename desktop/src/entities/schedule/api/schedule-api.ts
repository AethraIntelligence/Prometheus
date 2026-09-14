import type { RuntimeClient } from "../../../shared/api";
import type { ScheduleList } from "../model/types";

export const scheduleApi = {
  async all(client: RuntimeClient): Promise<ScheduleList> {
    return client.get<ScheduleList>("/api/schedules");
  },
};
