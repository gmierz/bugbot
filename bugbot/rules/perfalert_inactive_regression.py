# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import collections

from libmozdata.bugzilla import Bugzilla

from bugbot import logger, utils
from bugbot.bzcleaner import BzCleaner
from bugbot.user_activity import UserActivity, UserStatus


class PerfAlertInactiveRegression(BzCleaner):
    def __init__(self, nweeks=1):
        super().__init__()
        self.nweeks = nweeks
        self.extra_ni = {}
        self.private_regressor_ids: set[str] = set()

    def description(self):
        return f"PerfAlert regressions nag with {self.nweeks} week of inactivity"

    def handle_bug(self, bug, data):
        if len(bug["regressed_by"]) != 1:
            # either we don't have access to the regressor,
            # or there's more than one, either way leave things alone
            return

        data[str(bug["id"])] = {
            "regressor_id": bug["regressed_by"][0],
        }

        return bug

    def get_bz_params(self, date):
        start_date, _ = self.get_dates(date)

        fields = [
            "id",
            "regressed_by",
        ]

        # Find all bugs with regressed_by information which were open after start_date or
        # whose regressed_by field was set after start_date.
        params = {
            "include_fields": fields,
            "f3": "creation_ts",
            "o3": "greaterthan",
            "v3": "2024-10-01T00:00:00Z",
            "f1": "regressed_by",
            "o1": "isnotempty",
            "f2": "keywords",
            "o2": "allwords",
            "v2": ["regression", "perf-alert"],
            "f9": "days_elapsed",
            "o9": "greaterthan",
            "v9": self.nweeks * 7,
            "status": ["UNCONFIRMED", "NEW", "REOPENED"],
            "resolution": ["---"],
        }

        return params

    def retrieve_regressors(self, bugs):
        regressor_to_bugs = collections.defaultdict(list)
        for bug in bugs.values():
            regressor_to_bugs[bug["regressor_id"]].append(bug)

        def bug_handler(regressor_bug):
            if regressor_bug.get("groups"):
                regressor_bug_id = str(regressor_bug["id"])
                self.private_regressor_ids.add(regressor_bug_id)

            for bug in regressor_to_bugs[regressor_bug["id"]]:
                bug["regressor_author_email"] = regressor_bug["assigned_to"]
                bug["regressor_author_nickname"] = regressor_bug["assigned_to_detail"][
                    "nick"
                ]

        Bugzilla(
            bugids={bug["regressor_id"] for bug in bugs.values()},
            bughandler=bug_handler,
            include_fields=["id", "assigned_to", "groups"],
        ).get_data().wait()

    def filter_bugs(self, bugs):
        # TODO: Attempt to needinfo the triage owner instead of ignoring the bugs
        # Exclude bugs whose regressor author is nobody.
        for bug in list(bugs.values()):
            if utils.is_no_assignee(bug["regressor_author_email"]):
                logger.warning(
                    "Bug {}, regressor of bug {}, doesn't have an author".format(
                        bug["regressor_id"], bug["id"]
                    )
                )
                del bugs[bug["id"]]

        # Exclude bugs where the regressor author is inactive or blocked needinfo.
        # TODO: We can drop this when https://github.com/mozilla/bugbot/issues/1465 is implemented.
        users_info = UserActivity(include_fields=["groups", "requests"]).check_users(
            set(bug["regressor_author_email"] for bug in bugs.values()),
            keep_active=True,
            fetch_employee_info=True,
        )

        for bug_id, bug in list(bugs.items()):
            user_info = users_info[bug["regressor_author_email"]]
            if (
                user_info["status"] != UserStatus.ACTIVE
                or user_info["requests"]["needinfo"]["blocked"]
            ):
                del bugs[bug_id]

        return bugs

    def get_extra_for_needinfo_template(self):
        return self.extra_ni

    def get_extra_for_template(self):
        return {"nweeks": self.nweeks}

    def set_autofix(self, bugs):
        for bugid, info in bugs.items():
            self.extra_ni[bugid] = {
                "regressor_id": str(info["regressor_id"]),
                "nweeks": self.nweeks,
            }
            self.add_auto_ni(
                bugid,
                {
                    "mail": info["regressor_author_email"],
                    "nickname": info["regressor_author_nickname"],
                },
            )

    def get_bugs(self, *args, **kwargs):
        bugs = super().get_bugs(*args, **kwargs)
        self.retrieve_regressors(bugs)
        bugs = self.filter_bugs(bugs)
        self.set_autofix(bugs)
        return bugs

    def set_needinfo(self):
        res = super().set_needinfo()
        for bug_id, needinfo_action in res.items():
            needinfo_action["comment"]["is_private"] = (
                bug_id in self.private_regressor_ids
            )

        return res


if __name__ == "__main__":
    PerfAlertInactiveRegression().run()
