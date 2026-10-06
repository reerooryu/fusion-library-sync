"""The four actions, and what the dialog says about each one."""

SYNC = "Sync now"
PREVIEW = "Preview changes"
VERIFY = "Verify Integrity"
ADOPT = "Adopt existing library"

# The order they appear in the dialog. The first is the default.
ORDER = (SYNC, PREVIEW, VERIFY, ADOPT)

# Shown under the Action box, for whichever action is picked.
ABOUT = {
    SYNC: ("Gets what's new in the library. Shows you the list and asks "
           "before uploading. Never changes or deletes parts you already "
           "have."),
    PREVIEW: "Shows what Sync now would do. Changes nothing.",
    VERIFY: ("Reads every folder of your library. Puts back parts that were "
             "deleted, and lists duplicate names. Slow on a big library. Use "
             "it if something looks wrong."),
    ADOPT: ("For a library you imported yourself. Records the parts you "
            "already have, so Sync now won't upload them again. Uploads "
            "nothing. Do it once, then use Sync now."),
}
