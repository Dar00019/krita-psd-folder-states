from krita import Krita
from .psd_folder_states import PsdFolderStatesExtension

Krita.instance().addExtension(PsdFolderStatesExtension(Krita.instance()))