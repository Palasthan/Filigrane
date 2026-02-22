#ifndef MyAppVersion
  #ifexist "VERSION"
    #define _VersionFileHandle FileOpen("VERSION")
    #if _VersionFileHandle
      #define MyAppVersion FileRead(_VersionFileHandle)
      #expr FileClose(_VersionFileHandle)
    #else
      #define MyAppVersion "1.0.0"
    #endif
  #else
    #define MyAppVersion "1.0.0"
  #endif
#endif

[Setup]
AppId={{4A2EAB6F-1F6B-45F7-9B56-E59C28F43588}
AppName=Filigrane
AppVersion={#MyAppVersion}
AppPublisher=Palasthan
DefaultDirName={autopf}\Filigrane
DefaultGroupName=Filigrane
DisableProgramGroupPage=yes
OutputDir=installer-dist
OutputBaseFilename=Filigrane-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Filigrane.exe

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Creer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"; Flags: unchecked

[Files]
Source: "dist\Filigrane\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Filigrane"; Filename: "{app}\Filigrane.exe"
Name: "{autodesktop}\Filigrane"; Filename: "{app}\Filigrane.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Filigrane.exe"; Description: "Lancer Filigrane"; Flags: nowait postinstall skipifsilent
